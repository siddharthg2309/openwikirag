from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app.dependencies import get_object_storage, get_session
from apps.api.app.main import app
from openwikirag.application.documents import UploadValidationError, validate_upload
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    AuditEvent,
    Base,
    Document,
    DocumentVersion,
    IngestionJob,
    Membership,
    OutboxEvent,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError
from openwikirag.security.authorization import Role


class DocumentContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        user_id: UUID,
        subject: str,
        tenant_id: UUID,
        storage: LocalObjectStorage,
        root: Path,
    ) -> None:
        self.session_factory = session_factory
        self.user_id = user_id
        self.subject = subject
        self.tenant_id = tenant_id
        self.storage = storage
        self.root = root


@pytest.fixture
async def document_context(tmp_path: Path) -> AsyncIterator[DocumentContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'documents.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    subject = "oidc|document-user"
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("Document Tenant")
        user = await repository.create_user(
            "document@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.EDITOR)
        await session.commit()

    storage_root = tmp_path / "objects"
    storage = LocalObjectStorage(storage_root)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as test_session:
            yield test_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_object_storage] = lambda: storage
    yield DocumentContext(session_factory, user.id, subject, tenant.id, storage, storage_root)
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


async def upload(
    context: DocumentContext,
    *,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    tenant_id: UUID | None = None,
) -> Response:
    token = make_token(context.subject, tenant_id or context.tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "request-123"},
            files={"upload": (filename, data, content_type)},
        )


async def test_upload_stores_bytes_and_commits_canonical_metadata(
    document_context: DocumentContext,
) -> None:
    data = b"%PDF-1.7\nsource bytes"

    response = await upload(
        document_context,
        filename="../../Quarterly Plan.pdf",
        data=data,
        content_type="application/pdf",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "uploaded"
    assert payload["media_type"] == "application/pdf"
    assert payload["sanitized_filename"] == "Quarterly Plan.pdf"
    assert payload["checksum_sha256"]

    async with document_context.session_factory() as session:
        document = await session.scalar(select(Document))
        version = await session.scalar(select(DocumentVersion))
        job = await session.scalar(select(IngestionJob))
        outbox_event = await session.scalar(select(OutboxEvent))
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "document.upload")
        )

    assert document is not None
    assert version is not None
    assert job is not None
    assert outbox_event is not None
    assert audit is not None
    assert document.current_version_id == version.id
    assert version.version_number == 1
    assert version.byte_size == len(data)
    assert version.checksum_sha256 == payload["checksum_sha256"]
    assert version.source_object_key is not None
    assert await document_context.storage.get(object_key=version.source_object_key) == data
    assert audit.outcome == "success"
    assert outbox_event.event_type == "document.ingestion.requested"
    assert outbox_event.aggregate_id == str(job.id)
    assert outbox_event.payload_json == {
        "checksum_sha256": payload["checksum_sha256"],
        "document_id": str(document.id),
        "document_version_id": str(version.id),
        "ingestion_job_id": str(job.id),
        "pipeline_version": "ingestion-v1",
        "source_object_key": version.source_object_key,
    }


async def test_markdown_upload_accepts_generic_client_type_and_sanitizes_name(
    document_context: DocumentContext,
) -> None:
    response = await upload(
        document_context,
        filename="..\\team notes.md",
        data=b"# Heading\n\nBody",
    )

    assert response.status_code == 202
    assert response.json()["sanitized_filename"] == "team notes.md"
    assert response.json()["media_type"] == "text/markdown"


async def test_viewer_cannot_upload_and_failure_is_audited(
    document_context: DocumentContext,
) -> None:
    async with document_context.session_factory() as session:
        await session.execute(
            update(Membership)
            .where(Membership.tenant_id == document_context.tenant_id)
            .values(role=Role.VIEWER.value)
        )
        await session.commit()

    response = await upload(
        document_context,
        filename="notes.txt",
        data=b"not authorized",
        content_type="text/plain",
    )

    assert response.status_code == 403
    async with document_context.session_factory() as session:
        assert await session.scalar(select(Document)) is None
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "document.upload")
        )
    assert audit is not None
    assert audit.outcome == "failure"
    assert audit.metadata_json == {"error_code": "DOCUMENT_UPLOAD_FORBIDDEN"}


async def test_membershipless_tenant_token_cannot_upload(
    document_context: DocumentContext,
) -> None:
    async with document_context.session_factory() as session:
        other_tenant = await IdentityRepository(session).create_tenant("Other Tenant")
        await session.commit()

    response = await upload(
        document_context,
        filename="notes.txt",
        data=b"not authorized",
        content_type="text/plain",
        tenant_id=other_tenant.id,
    )

    assert response.status_code == 403
    async with document_context.session_factory() as session:
        assert await session.scalar(select(Document)) is None


@pytest.mark.parametrize(
    ("filename", "declared_media_type", "data", "max_upload_bytes", "code"),
    [
        ("notes.exe", "application/octet-stream", b"MZ", 100, "DOCUMENT_UNSUPPORTED_MEDIA_TYPE"),
        ("notes.txt", "text/plain", b"abc", 2, "DOCUMENT_TOO_LARGE"),
        ("notes.txt", "text/plain", b"\xff", 100, "DOCUMENT_CONTENT_MISMATCH"),
        ("notes.pdf", "text/plain", b"%PDF-1.7", 100, "DOCUMENT_DECLARED_TYPE_MISMATCH"),
    ],
)
def test_upload_validation_rejects_unsafe_or_mismatched_input(
    filename: str,
    declared_media_type: str,
    data: bytes,
    max_upload_bytes: int,
    code: str,
) -> None:
    with pytest.raises(UploadValidationError) as error:
        validate_upload(
            filename=filename,
            declared_media_type=declared_media_type,
            data=data,
            max_upload_bytes=max_upload_bytes,
        )

    assert error.value.code == code


async def test_storage_failure_does_not_create_document_metadata(
    document_context: DocumentContext,
) -> None:
    class FailingStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise ObjectStorageError("storage unavailable")

        async def get(self, *, object_key: str) -> bytes:
            raise AssertionError("get should not be called")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    app.dependency_overrides[get_object_storage] = lambda: FailingStorage()
    response = await upload(
        document_context,
        filename="notes.txt",
        data=b"source",
        content_type="text/plain",
    )

    assert response.status_code == 503
    async with document_context.session_factory() as session:
        assert await session.scalar(select(Document)) is None
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "document.upload")
        )
    assert audit is not None
    assert audit.metadata_json == {"error_code": "DOCUMENT_OBJECT_WRITE_FAILED"}


async def test_metadata_failure_removes_already_written_object(
    document_context: DocumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openwikirag.infrastructure.repositories import documents as document_repository

    async def fail_metadata(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        document_repository.DocumentRepository,
        "create_document_version",
        fail_metadata,
    )

    response = await upload(
        document_context,
        filename="notes.txt",
        data=b"source",
        content_type="text/plain",
    )

    assert response.status_code == 500
    assert not any(path.is_file() for path in document_context.root.rglob("*"))
    async with document_context.session_factory() as session:
        assert await session.scalar(select(Document)) is None
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "document.upload")
        )
    assert audit is not None
    assert audit.metadata_json == {
        "error_code": "DOCUMENT_METADATA_WRITE_FAILED",
        "object_cleanup_failed": False,
    }


async def test_local_storage_rejects_path_traversal(document_context: DocumentContext) -> None:
    with pytest.raises(ObjectStorageError):
        await document_context.storage.put(
            object_key="../outside",
            data=b"secret",
            content_type="text/plain",
        )
