"""Persistence proof for canonical normalized document artifacts."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.extraction import InvalidTextEncodingError, MalformedPdfError
from openwikirag.application.normalized_artifacts import (
    DocumentVersionNotFoundError,
    NormalizedArtifactConflictError,
    NormalizedArtifactPersistenceError,
    NormalizedArtifactService,
    NormalizedArtifactStorageError,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    Document,
    DocumentVersion,
    NormalizedDocumentArtifact,
    Tenant,
)
from openwikirag.infrastructure.repositories import normalized_artifacts as artifact_repository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError


class ArtifactContext:
    def __init__(
        self,
        session: AsyncSession,
        storage: LocalObjectStorage,
        root: Path,
        tenant_id: UUID,
        document_version_id: UUID,
        source_object_key: str,
    ) -> None:
        self.session = session
        self.storage = storage
        self.root = root
        self.tenant_id = tenant_id
        self.document_version_id = document_version_id
        self.source_object_key = source_object_key


@pytest.fixture
async def artifact_context(tmp_path: Path) -> AsyncIterator[ArtifactContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'artifacts.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    storage_root = tmp_path / "objects"
    storage = LocalObjectStorage(storage_root)
    async with session_factory() as session:
        tenant, version, source_object_key = await create_source_version(
            session,
            storage,
            source_type="markdown",
            data=b"# Overview\r\nBody\n",
        )
        yield ArtifactContext(
            session,
            storage,
            storage_root,
            tenant.id,
            version.id,
            source_object_key,
        )
    await engine.dispose()


async def create_source_version(
    session: AsyncSession,
    storage: LocalObjectStorage,
    *,
    source_type: str,
    data: bytes,
) -> tuple[Tenant, DocumentVersion, str]:
    tenant = Tenant(name=f"Artifact Tenant {uuid4().hex}")
    session.add(tenant)
    await session.flush()
    document = Document(tenant_id=tenant.id, title="Artifact Source", source_type=source_type)
    session.add(document)
    await session.flush()
    source_object_key = f"tenants/{tenant.id}/documents/{document.id}/source"
    version = DocumentVersion(
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=1,
        original_filename=f"source.{source_type}",
        sanitized_filename=f"source.{source_type}",
        source_type=source_type,
        media_type="text/markdown" if source_type == "markdown" else "text/plain",
        byte_size=len(data),
        checksum_sha256="a" * 64,
        source_object_key=source_object_key,
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id
    await storage.put(object_key=source_object_key, data=data, content_type=version.media_type)
    await session.commit()
    return tenant, version, source_object_key


async def test_persist_stores_canonical_json_outside_postgres(
    artifact_context: ArtifactContext,
) -> None:
    result = await NormalizedArtifactService(
        artifact_context.session,
        artifact_context.storage,
    ).persist(
        tenant_id=artifact_context.tenant_id,
        document_version_id=artifact_context.document_version_id,
    )

    assert result.reused is False
    assert result.character_count == len("# Overview\nBody\n")
    assert result.span_count == 2
    assert str(artifact_context.tenant_id) in result.artifact_object_key
    assert str(artifact_context.document_version_id) in result.artifact_object_key
    stored = json.loads(
        (await artifact_context.storage.get(object_key=result.artifact_object_key)).decode("utf-8")
    )
    assert stored["text"] == "# Overview\nBody\n"
    assert stored["parser_name"] == "markdown"
    assert stored["spans"][0]["section_path"] == ["Overview"]

    metadata = await artifact_context.session.get(NormalizedDocumentArtifact, result.artifact_id)
    assert metadata is not None
    assert metadata.content_checksum_sha256 == result.checksum_sha256
    assert metadata.artifact_object_key == result.artifact_object_key


async def test_rerun_reuses_one_immutable_metadata_row(artifact_context: ArtifactContext) -> None:
    service = NormalizedArtifactService(artifact_context.session, artifact_context.storage)

    first = await service.persist(
        tenant_id=artifact_context.tenant_id,
        document_version_id=artifact_context.document_version_id,
    )
    second = await service.persist(
        tenant_id=artifact_context.tenant_id,
        document_version_id=artifact_context.document_version_id,
    )

    assert first.reused is False
    assert second.reused is True
    assert second.artifact_id == first.artifact_id
    count = await artifact_context.session.scalar(
        select(func.count(NormalizedDocumentArtifact.id))
    )
    assert count == 1


async def test_foreign_or_missing_version_is_not_readable(
    artifact_context: ArtifactContext,
) -> None:
    service = NormalizedArtifactService(artifact_context.session, artifact_context.storage)

    with pytest.raises(DocumentVersionNotFoundError):
        await service.persist(
            tenant_id=uuid4(),
            document_version_id=artifact_context.document_version_id,
        )
    with pytest.raises(DocumentVersionNotFoundError):
        await service.persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=uuid4(),
        )
    count = await artifact_context.session.scalar(
        select(func.count(NormalizedDocumentArtifact.id))
    )
    assert count == 0


async def test_extraction_failures_leave_no_normalized_metadata(
    artifact_context: ArtifactContext,
) -> None:
    service = NormalizedArtifactService(artifact_context.session, artifact_context.storage)
    unsupported_tenant, unsupported_version, _ = await create_source_version(
        artifact_context.session,
        artifact_context.storage,
        source_type="pdf",
        data=b"%PDF-1.7",
    )
    with pytest.raises(MalformedPdfError):
        await service.persist(
            tenant_id=unsupported_tenant.id,
            document_version_id=unsupported_version.id,
        )

    invalid_text_tenant, invalid_text_version, _ = await create_source_version(
        artifact_context.session,
        artifact_context.storage,
        source_type="text",
        data=b"\xff",
    )
    with pytest.raises(InvalidTextEncodingError):
        await service.persist(
            tenant_id=invalid_text_tenant.id,
            document_version_id=invalid_text_version.id,
        )

    count = await artifact_context.session.scalar(
        select(func.count(NormalizedDocumentArtifact.id))
    )
    assert count == 0


async def test_source_read_and_normalized_write_fail_without_metadata(
    artifact_context: ArtifactContext,
) -> None:
    class FailingReadStorage:
        async def get(self, *, object_key: str) -> bytes:
            raise ObjectStorageError("source unavailable")

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise AssertionError("put should not be called")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    with pytest.raises(NormalizedArtifactStorageError):
        await NormalizedArtifactService(
            artifact_context.session,
            FailingReadStorage(),
        ).persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=artifact_context.document_version_id,
        )

    class FailingWriteStorage:
        async def get(self, *, object_key: str) -> bytes:
            return await artifact_context.storage.get(object_key=object_key)

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise ObjectStorageError("output unavailable")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    with pytest.raises(NormalizedArtifactStorageError):
        await NormalizedArtifactService(
            artifact_context.session,
            FailingWriteStorage(),
        ).persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=artifact_context.document_version_id,
        )
    count = await artifact_context.session.scalar(
        select(func.count(NormalizedDocumentArtifact.id))
    )
    assert count == 0


async def test_metadata_failure_cleans_the_new_artifact_object(
    artifact_context: ArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_create(*args: object, **kwargs: object) -> NormalizedDocumentArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        artifact_repository.NormalizedArtifactRepository,
        "create",
        fail_create,
    )

    with pytest.raises(NormalizedArtifactPersistenceError) as error:
        await NormalizedArtifactService(
            artifact_context.session,
            artifact_context.storage,
        ).persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=artifact_context.document_version_id,
        )

    assert error.value.cleanup_failed is False
    assert not any(path.suffix == ".json" for path in artifact_context.root.rglob("*"))
    count = await artifact_context.session.scalar(
        select(func.count(NormalizedDocumentArtifact.id))
    )
    assert count == 0


async def test_metadata_failure_reports_when_cleanup_cannot_remove_object(
    artifact_context: ArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCleanupStorage:
        async def get(self, *, object_key: str) -> bytes:
            return await artifact_context.storage.get(object_key=object_key)

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            await artifact_context.storage.put(
                object_key=object_key,
                data=data,
                content_type=content_type,
            )

        async def delete(self, *, object_key: str) -> None:
            raise ObjectStorageError("cleanup unavailable")

    async def fail_create(*args: object, **kwargs: object) -> NormalizedDocumentArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        artifact_repository.NormalizedArtifactRepository,
        "create",
        fail_create,
    )

    with pytest.raises(NormalizedArtifactPersistenceError) as error:
        await NormalizedArtifactService(
            artifact_context.session,
            FailingCleanupStorage(),
        ).persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=artifact_context.document_version_id,
        )

    assert error.value.cleanup_failed is True
    assert any(path.suffix == ".json" for path in artifact_context.root.rglob("*"))


async def test_same_parser_identity_cannot_overwrite_immutable_output(
    artifact_context: ArtifactContext,
) -> None:
    service = NormalizedArtifactService(artifact_context.session, artifact_context.storage)
    first = await service.persist(
        tenant_id=artifact_context.tenant_id,
        document_version_id=artifact_context.document_version_id,
    )
    await artifact_context.storage.put(
        object_key=artifact_context.source_object_key,
        data=b"# Changed\n",
        content_type="text/markdown",
    )

    with pytest.raises(NormalizedArtifactConflictError):
        await service.persist(
            tenant_id=artifact_context.tenant_id,
            document_version_id=artifact_context.document_version_id,
        )

    metadata = await artifact_context.session.get(NormalizedDocumentArtifact, first.artifact_id)
    assert metadata is not None
    assert metadata.content_checksum_sha256 == first.checksum_sha256
