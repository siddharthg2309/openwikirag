"""API proof for authenticated, tenant-scoped WikiRAG page reads."""

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
    WikiPageArtifact,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
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
        tenant_id: UUID,
        page_artifact_id: UUID,
        foreign_page_artifact_id: UUID,
        storage: LocalObjectStorage,
        root: Path,
    ) -> None:
        self.session_factory = session_factory
        self.subject = subject
        self.tenant_id = tenant_id
        self.page_artifact_id = page_artifact_id
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
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("Wiki Page Tenant")
        foreign_tenant = await repository.create_tenant("Foreign Wiki Tenant")
        user = await repository.create_user(
            f"wiki-page-{uuid4().hex}@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.VIEWER)
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
        tenant.id,
        persisted_page.artifact_id,
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
