"""API proof for authenticated WikiRAG page reads and review actions."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app.dependencies import get_object_storage, get_session
from apps.api.app.main import app
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.normalized_artifacts import NormalizedArtifactService
from openwikirag.application.wiki import WikiPageBuilder
from openwikirag.application.wiki_generation import (
    StructuredWikiGenerator,
    WikiGenerationRequest,
)
from openwikirag.application.wiki_generation_artifacts import (
    WikiGenerationArtifactService,
)
from openwikirag.application.wiki_page_artifacts import WikiPageArtifactService
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    AuditEvent,
    Base,
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxEvent,
    WikiPageArtifact,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.repositories.wiki_page_artifacts import (
    WikiPageArtifactRepository,
)
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError
from openwikirag.security.authorization import Role

CONFIG_HASH = "c" * 64
SOURCE_DATA = b"# Overview\nOpenWikiRAG uses citations.\n"


class StaticProvider:
    provider_identity = "fake-provider-v1"

    def generate(self, *, request: WikiGenerationRequest) -> object:
        value = "OpenWikiRAG uses citations."
        start = request.document_text.index(value)
        return {
            "summary": {
                "text": "The document describes citation-backed knowledge.",
                "evidence": [
                    {
                        "value": value,
                        "raw_text": value,
                        "normalized_start_char": start,
                        "normalized_end_char": start + len(value),
                        "section_path": ["Overview"],
                        "page_number": None,
                    }
                ],
            },
            "definitions": [],
            "references": [],
        }


class WikiPageApiContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        subject: str,
        editor_subject: str,
        tenant_id: UUID,
        page_artifact_id: UUID,
        second_page_artifact_id: UUID,
        foreign_page_artifact_id: UUID,
        storage: LocalObjectStorage,
        root: Path,
    ) -> None:
        self.session_factory = session_factory
        self.subject = subject
        self.editor_subject = editor_subject
        self.tenant_id = tenant_id
        self.page_artifact_id = page_artifact_id
        self.second_page_artifact_id = second_page_artifact_id
        self.foreign_page_artifact_id = foreign_page_artifact_id
        self.storage = storage
        self.root = root


@pytest.fixture
async def wiki_page_api_context(tmp_path: Path) -> AsyncIterator[WikiPageApiContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wiki-pages.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    root = tmp_path / "objects"
    storage = LocalObjectStorage(root)
    subject = f"oidc|wiki-page-reader-{uuid4().hex}"
    editor_subject = f"oidc|wiki-page-editor-{uuid4().hex}"
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("Wiki Page Tenant")
        foreign_tenant = await repository.create_tenant("Foreign Wiki Tenant")
        user = await repository.create_user(
            f"wiki-page-{uuid4().hex}@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.VIEWER)
        editor_user = await repository.create_user(
            f"wiki-editor-{uuid4().hex}@example.com",
            auth_provider_subject=editor_subject,
        )
        await repository.add_membership(tenant.id, editor_user.id, Role.EDITOR)
        document = Document(
            tenant_id=tenant.id,
            title="Wiki Page Source",
            source_type="markdown",
        )
        session.add(document)
        await session.flush()
        source_object_key = f"tenants/{tenant.id}/documents/{document.id}/source"
        version = DocumentVersion(
            tenant_id=tenant.id,
            document_id=document.id,
            version_number=1,
            original_filename="source.md",
            sanitized_filename="source.md",
            source_type="markdown",
            media_type="text/markdown",
            byte_size=len(SOURCE_DATA),
            checksum_sha256="a" * 64,
            source_object_key=source_object_key,
        )
        session.add(version)
        await session.flush()
        document.current_version_id = version.id
        await storage.put(
            object_key=source_object_key,
            data=SOURCE_DATA,
            content_type="text/markdown",
        )
        await session.commit()

        persisted_source = await NormalizedArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
        )
        normalized_document = MarkdownExtractor().extract(SOURCE_DATA)
        metadata = DeterministicMetadataExtractor().extract(document=normalized_document)
        page = WikiPageBuilder().build(document=normalized_document, metadata=metadata)
        generation = StructuredWikiGenerator(provider=StaticProvider()).generate(
            document=normalized_document,
            page=page,
            config_hash=CONFIG_HASH,
        )
        persisted_generation = await WikiGenerationArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            result=generation,
        )
        persisted_page = await WikiPageArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            generation_artifact_id=persisted_generation.artifact_id,
            page=page,
            generation=generation,
        )

        second_page = WikiPageArtifact(
            tenant_id=tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            generation_artifact_id=persisted_generation.artifact_id,
            page_checksum="b" * 64,
            generation_result_checksum_sha256=persisted_page.generation_result_checksum_sha256,
            content_checksum_sha256="d" * 64,
            artifact_object_key="tenant/unused-page-2.json",
        )
        session.add(second_page)

        foreign_page = WikiPageArtifact(
            tenant_id=foreign_tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            generation_artifact_id=persisted_generation.artifact_id,
            page_checksum="f" * 64,
            generation_result_checksum_sha256=persisted_page.generation_result_checksum_sha256,
            content_checksum_sha256="e" * 64,
            artifact_object_key="foreign/unused-page.json",
        )
        session.add(foreign_page)
        await session.commit()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as test_session:
            yield test_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_object_storage] = lambda: storage
    yield WikiPageApiContext(
        session_factory,
        subject,
        editor_subject,
        tenant.id,
        persisted_page.artifact_id,
        second_page.id,
        foreign_page.id,
        storage,
        root,
    )
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_object_storage, None)
    await engine.dispose()


def make_token(subject: str, tenant_id: UUID) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "tenant_id": str(tenant_id),
        "iss": get_settings().jwt_issuer,
        "aud": get_settings().jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    return jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")


async def get_page(
    context: WikiPageApiContext,
    artifact_id: UUID,
    *,
    token: str | None = "valid",
) -> Response:
    transport = ASGITransport(app=app)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/v1/wiki/pages/{artifact_id}", headers=headers)


async def review_page(
    context: WikiPageApiContext,
    artifact_id: UUID,
    target_status: str,
    *,
    token: str,
) -> Response:
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/api/v1/wiki/pages/{artifact_id}/review",
            headers=headers,
            json={"status": target_status},
        )


async def list_pages(
    context: WikiPageApiContext,
    *,
    token: str | None,
    review_status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> Response:
    transport = ASGITransport(app=app)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    params: dict[str, str | int] = {}
    if review_status is not None:
        params["review_status"] = review_status
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/wiki/pages", headers=headers, params=params)


async def regenerate_page(
    context: WikiPageApiContext,
    artifact_id: UUID,
    *,
    token: str,
    reason: str | None = None,
    request_id: str | None = None,
) -> Response:
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        json_body = {"reason": reason} if reason is not None else None
        return await client.post(
            f"/api/v1/wiki/pages/{artifact_id}/regenerate",
            headers=headers,
            json=json_body,
        )


async def test_viewer_reads_complete_page_and_audits_success(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_id"] == str(wiki_page_api_context.page_artifact_id)
    assert payload["tenant_id"] == str(wiki_page_api_context.tenant_id)
    assert payload["page"]["title"] == "Overview"
    assert payload["page"]["sections"][0]["title"] == "Overview"
    assert payload["generation"]["content"]["summary"]["text"]
    assert payload["review_status"] == "draft"

    async with wiki_page_api_context.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "wiki.page.read",
                AuditEvent.resource_id == str(wiki_page_api_context.page_artifact_id),
            )
        )
    assert audit is not None
    assert audit.outcome == "success"


async def test_foreign_and_missing_pages_are_indistinguishable(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id)

    foreign_response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.foreign_page_artifact_id,
        token=token,
    )
    missing_response = await get_page(wiki_page_api_context, uuid4(), token=token)

    assert foreign_response.status_code == 404
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()


async def test_page_read_requires_bearer_auth(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=None,
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_missing_object_maps_to_retryable_storage_error(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        object_path = wiki_page_api_context.root / artifact.artifact_object_key
        object_path.unlink()

    response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
    )

    assert response.status_code == 503


async def test_corrupted_object_fails_closed_without_success_audit(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        await wiki_page_api_context.storage.put(
            object_key=artifact.artifact_object_key,
            data=b"corrupted",
            content_type="application/json",
        )

    response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
    )

    assert response.status_code == 500
    async with wiki_page_api_context.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "wiki.page.read",
                AuditEvent.resource_id == str(wiki_page_api_context.page_artifact_id),
            )
        )
    assert audit is None


async def test_malformed_json_with_matching_checksum_fails_schema_validation(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        data = json.dumps({"schema_version": "wiki-generated-page-v1"}).encode()
        artifact.content_checksum_sha256 = hashlib.sha256(data).hexdigest()
        await wiki_page_api_context.storage.put(
            object_key=artifact.artifact_object_key,
            data=data,
            content_type="application/json",
        )
        await session.commit()

    response = await get_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
    )

    assert response.status_code == 500


async def test_storage_failure_does_not_create_success_audit(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    class FailingReadStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise AssertionError("put should not be called")

        async def get(self, *, object_key: str) -> bytes:
            raise ObjectStorageError("object unavailable")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    app.dependency_overrides[get_object_storage] = lambda: FailingReadStorage()
    try:
        response = await get_page(
            wiki_page_api_context,
            wiki_page_api_context.page_artifact_id,
            token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
        )
    finally:
        app.dependency_overrides[get_object_storage] = lambda: wiki_page_api_context.storage

    assert response.status_code == 503


async def test_editor_can_transition_page_and_preserve_immutable_object(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        object_bytes = await wiki_page_api_context.storage.get(
            object_key=artifact.artifact_object_key
        )

    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "needs_review",
        token=token,
    )

    assert response.status_code == 200
    assert response.json()["previous_status"] == "draft"
    assert response.json()["review_status"] == "needs_review"
    assert response.json()["changed"] is True
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        assert artifact.review_status == "needs_review"
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "wiki.page.review",
                AuditEvent.resource_id == str(wiki_page_api_context.page_artifact_id),
            )
        )
    assert audit is not None
    assert audit.metadata_json == {
        "from_status": "draft",
        "to_status": "needs_review",
        "changed": True,
    }
    assert (
        await wiki_page_api_context.storage.get(object_key=artifact.artifact_object_key)
        == object_bytes
    )


async def test_review_transition_supports_approval_and_reopening(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )

    for target_status in ("needs_review", "approved", "needs_review"):
        response = await review_page(
            wiki_page_api_context,
            wiki_page_api_context.page_artifact_id,
            target_status,
            token=token,
        )
        assert response.status_code == 200
        assert response.json()["review_status"] == target_status


async def test_same_review_status_is_idempotent(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )

    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "draft",
        token=token,
    )

    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": str(wiki_page_api_context.page_artifact_id),
        "tenant_id": str(wiki_page_api_context.tenant_id),
        "previous_status": "draft",
        "review_status": "draft",
        "changed": False,
    }


async def test_viewer_cannot_change_review_status(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "needs_review",
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
    )

    assert response.status_code == 403
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
    assert artifact is not None
    assert artifact.review_status == "draft"


async def test_editor_queues_regeneration_with_outbox_and_audit(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    response = await regenerate_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(
            wiki_page_api_context.editor_subject,
            wiki_page_api_context.tenant_id,
        ),
        reason="Refresh the structured page after the prompt update.",
        request_id="regen-request-1",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["source_page_artifact_id"] == str(wiki_page_api_context.page_artifact_id)
    assert payload["tenant_id"] == str(wiki_page_api_context.tenant_id)
    assert payload["status"] == "pending"

    async with wiki_page_api_context.session_factory() as session:
        job = await session.get(IngestionJob, UUID(payload["job_id"]))
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == payload["job_id"],
                OutboxEvent.event_type == "wiki.page.regeneration.requested",
            )
        )
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "wiki.page.regeneration.requested",
                AuditEvent.resource_id == str(wiki_page_api_context.page_artifact_id),
            )
        )
        original_page = await session.get(
            WikiPageArtifact,
            wiki_page_api_context.page_artifact_id,
        )

    assert job is not None
    assert job.job_type == "wiki_regeneration"
    assert str(job.document_version_id) == payload["document_version_id"]
    assert job.request_id == "regen-request-1"
    assert event is not None
    assert event.payload_json == {
        "ingestion_job_id": payload["job_id"],
        "page_artifact_id": str(wiki_page_api_context.page_artifact_id),
        "source_page_checksum": original_page.page_checksum if original_page else None,
    }
    assert audit is not None
    assert audit.metadata_json == {
        "ingestion_job_id": payload["job_id"],
        "document_version_id": payload["document_version_id"],
        "reason": "Refresh the structured page after the prompt update.",
    }
    assert original_page is not None
    assert original_page.review_status == "draft"


async def test_viewer_cannot_queue_regeneration(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    response = await regenerate_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(
            wiki_page_api_context.subject,
            wiki_page_api_context.tenant_id,
        ),
    )

    assert response.status_code == 403
    async with wiki_page_api_context.session_factory() as session:
        jobs = list(
            (
                await session.scalars(
                    select(IngestionJob).where(IngestionJob.job_type == "wiki_regeneration")
                )
            ).all()
        )
    assert jobs == []


async def test_regeneration_audit_failure_rolls_back_job_and_outbox(
    wiki_page_api_context: WikiPageApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openwikirag.infrastructure.repositories.audit import AuditRepository

    async def fail_record(self: AuditRepository, **kwargs: Any) -> AuditEvent:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditRepository, "record", fail_record)
    response = await regenerate_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        token=make_token(
            wiki_page_api_context.editor_subject,
            wiki_page_api_context.tenant_id,
        ),
    )

    assert response.status_code == 500
    async with wiki_page_api_context.session_factory() as session:
        jobs = list(
            (
                await session.scalars(
                    select(IngestionJob).where(IngestionJob.job_type == "wiki_regeneration")
                )
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "wiki.page.regeneration.requested"
                    )
                )
            ).all()
        )
    assert jobs == []
    assert events == []


async def test_foreign_and_missing_regeneration_targets_are_indistinguishable(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )
    foreign_response = await regenerate_page(
        wiki_page_api_context,
        wiki_page_api_context.foreign_page_artifact_id,
        token=token,
    )
    missing_response = await regenerate_page(
        wiki_page_api_context,
        uuid4(),
        token=token,
    )

    assert foreign_response.status_code == 404
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()


async def test_foreign_and_missing_review_targets_are_indistinguishable(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )
    foreign_response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.foreign_page_artifact_id,
        "needs_review",
        token=token,
    )
    missing_response = await review_page(
        wiki_page_api_context,
        uuid4(),
        "needs_review",
        token=token,
    )

    assert foreign_response.status_code == 404
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()


async def test_invalid_review_transition_does_not_change_metadata(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )
    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "approved",
        token=token,
    )

    assert response.status_code == 409
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        review_audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "wiki.page.review")
        )
    assert artifact is not None
    assert artifact.review_status == "draft"
    assert review_audit is None


async def test_stale_compare_and_set_cannot_overwrite_current_status(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    async with wiki_page_api_context.session_factory() as session:
        repository = WikiPageArtifactRepository(session)
        updated = await repository.compare_and_set_review_status(
            tenant_id=wiki_page_api_context.tenant_id,
            artifact_id=wiki_page_api_context.page_artifact_id,
            expected_status="needs_review",
            new_status="approved",
        )
        await session.rollback()

    assert updated is False
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
    assert artifact is not None
    assert artifact.review_status == "draft"


async def test_audit_failure_rolls_back_review_status(
    wiki_page_api_context: WikiPageApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openwikirag.infrastructure.repositories.audit import AuditRepository

    async def fail_record(self: AuditRepository, **kwargs: Any) -> AuditEvent:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditRepository, "record", fail_record)
    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "needs_review",
        token=make_token(
            wiki_page_api_context.editor_subject,
            wiki_page_api_context.tenant_id,
        ),
    )

    assert response.status_code == 500
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
    assert artifact is not None
    assert artifact.review_status == "draft"


async def test_unknown_persisted_review_status_fails_closed(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    async with wiki_page_api_context.session_factory() as session:
        artifact = await session.get(WikiPageArtifact, wiki_page_api_context.page_artifact_id)
        assert artifact is not None
        artifact.review_status = "retired"
        await session.commit()

    response = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "needs_review",
        token=make_token(
            wiki_page_api_context.editor_subject,
            wiki_page_api_context.tenant_id,
        ),
    )

    assert response.status_code == 500


async def test_viewer_lists_only_tenant_metadata_with_stable_pagination(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    token = make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id)

    first_response = await list_pages(
        wiki_page_api_context,
        token=token,
        limit=1,
        offset=0,
    )
    second_response = await list_pages(
        wiki_page_api_context,
        token=token,
        limit=1,
        offset=1,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["limit"] == 1
    assert first_payload["offset"] == 0
    assert first_payload["has_more"] is True
    assert second_payload["has_more"] is False
    listed_ids = {
        first_payload["items"][0]["artifact_id"],
        second_payload["items"][0]["artifact_id"],
    }
    assert listed_ids == {
        str(wiki_page_api_context.page_artifact_id),
        str(wiki_page_api_context.second_page_artifact_id),
    }
    assert all(
        item["tenant_id"] == str(wiki_page_api_context.tenant_id)
        for payload in (first_payload, second_payload)
        for item in payload["items"]
    )
    assert "artifact_object_key" not in first_payload["items"][0]


async def test_listing_filters_review_status_and_audits_success(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    editor_token = make_token(
        wiki_page_api_context.editor_subject,
        wiki_page_api_context.tenant_id,
    )
    viewer_token = make_token(
        wiki_page_api_context.subject,
        wiki_page_api_context.tenant_id,
    )
    transition = await review_page(
        wiki_page_api_context,
        wiki_page_api_context.page_artifact_id,
        "needs_review",
        token=editor_token,
    )
    assert transition.status_code == 200

    response = await list_pages(
        wiki_page_api_context,
        token=viewer_token,
        review_status="needs_review",
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["artifact_id"] for item in payload["items"]] == [
        str(wiki_page_api_context.page_artifact_id)
    ]
    assert payload["items"][0]["review_status"] == "needs_review"
    async with wiki_page_api_context.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "wiki.page.list")
        )
    assert audit is not None
    assert audit.metadata_json == {
        "review_status": "needs_review",
        "limit": 50,
        "offset": 0,
        "count": 1,
        "has_more": False,
    }


async def test_empty_listing_returns_success_without_object_storage_read(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    class FailingStorage:
        async def get(self, *, object_key: str) -> bytes:
            raise AssertionError("list should not read object storage")

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise AssertionError("list should not write object storage")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("list should not delete object storage")

    app.dependency_overrides[get_object_storage] = lambda: FailingStorage()
    try:
        response = await list_pages(
            wiki_page_api_context,
            token=make_token(
                wiki_page_api_context.subject,
                wiki_page_api_context.tenant_id,
            ),
            review_status="approved",
        )
    finally:
        app.dependency_overrides[get_object_storage] = lambda: wiki_page_api_context.storage

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["has_more"] is False


async def test_listing_requires_auth_and_validates_query_window(
    wiki_page_api_context: WikiPageApiContext,
) -> None:
    unauthenticated = await list_pages(wiki_page_api_context, token=None)
    invalid_limit = await list_pages(
        wiki_page_api_context,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
        limit=101,
    )
    invalid_offset = await list_pages(
        wiki_page_api_context,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
        offset=-1,
    )
    invalid_status = await list_pages(
        wiki_page_api_context,
        token=make_token(wiki_page_api_context.subject, wiki_page_api_context.tenant_id),
        review_status="retired",
    )

    assert unauthenticated.status_code == 401
    assert invalid_limit.status_code == 422
    assert invalid_offset.status_code == 422
    assert invalid_status.status_code == 422
