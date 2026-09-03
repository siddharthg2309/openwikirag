"""Proof for immutable, tenant-scoped chunk-manifest persistence."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.chunk_artifacts import (
    ChunkManifestConflictError,
    ChunkManifestInputError,
    ChunkManifestPersistenceError,
    ChunkManifestService,
    ChunkManifestSourceMismatchError,
    ChunkManifestSourceNotFoundError,
    ChunkManifestStorageError,
)
from openwikirag.application.chunking import ChunkingConfig, ChunkingResult, HierarchicalChunker
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.normalized_artifacts import NormalizedArtifactService
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    ChunkManifestArtifact,
    Document,
    DocumentVersion,
    Tenant,
)
from openwikirag.infrastructure.repositories import chunk_artifacts as artifact_repository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError

SOURCE_DATA = (
    b"# Overview\nOpenWikiRAG uses stable citations and deterministic chunks.\n"
    b"# Retrieval\nDense and sparse signals cooperate for evidence discovery.\n"
)


class ChunkArtifactContext:
    def __init__(
        self,
        session: AsyncSession,
        storage: LocalObjectStorage,
        root: Path,
        tenant_id: UUID,
        foreign_tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        result: ChunkingResult,
        config: ChunkingConfig,
    ) -> None:
        self.session = session
        self.storage = storage
        self.root = root
        self.tenant_id = tenant_id
        self.foreign_tenant_id = foreign_tenant_id
        self.document_version_id = document_version_id
        self.normalized_artifact_id = normalized_artifact_id
        self.result = result
        self.config = config


@pytest.fixture
async def chunk_artifact_context(
    tmp_path: Path,
) -> AsyncIterator[ChunkArtifactContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'chunk-artifacts.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    root = tmp_path / "objects"
    storage = LocalObjectStorage(root)
    async with session_factory() as session:
        tenant = Tenant(name=f"Chunk Tenant {uuid4().hex}")
        foreign_tenant = Tenant(name=f"Foreign Chunk Tenant {uuid4().hex}")
        session.add_all((tenant, foreign_tenant))
        await session.flush()
        document = Document(tenant_id=tenant.id, title="Chunk Source", source_type="markdown")
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
        document_model = MarkdownExtractor().extract(SOURCE_DATA)
        config = ChunkingConfig(
            parent_max_tokens=40,
            parent_max_characters=240,
            child_max_tokens=5,
            child_max_characters=80,
            child_overlap_tokens=1,
        )
        result = HierarchicalChunker(config).chunk(document_model)
        yield ChunkArtifactContext(
            session=session,
            storage=storage,
            root=root,
            tenant_id=tenant.id,
            foreign_tenant_id=foreign_tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            result=result,
            config=config,
        )
    await engine.dispose()


def _service(context: ChunkArtifactContext) -> ChunkManifestService:
    return ChunkManifestService(context.session, context.storage)


def _has_chunk_manifest_object(context: ChunkArtifactContext) -> bool:
    """Return whether the test storage contains a persisted chunk manifest."""

    return any(path.is_file() and "chunks" in path.parts for path in context.root.rglob("*"))


async def test_persist_stores_manifest_json_and_metadata(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    result = await _service(context).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )

    assert result.reused is False
    assert result.chunk_count == len(context.result.chunks)
    assert result.parent_count > 0
    assert result.child_count > 0
    assert str(context.tenant_id) in result.artifact_object_key
    assert str(context.document_version_id) in result.artifact_object_key
    stored = json.loads(
        (await context.storage.get(object_key=result.artifact_object_key)).decode("utf-8")
    )
    assert stored["schema_version"] == "chunk-manifest-v1"
    assert stored["chunk_schema_version"] == "chunk-v1"
    assert stored["source_artifact_checksum"] == context.result.chunks[0].source_artifact_checksum
    assert stored["chunking_config_checksum"] == context.config.checksum_sha256
    assert stored["chunk_count"] == result.chunk_count
    assert stored["chunks"][0]["text"]

    metadata = await context.session.get(ChunkManifestArtifact, result.artifact_id)
    assert metadata is not None
    assert metadata.manifest_checksum_sha256 == result.manifest_checksum_sha256
    assert metadata.chunking_config_checksum == context.config.checksum_sha256
    assert metadata.artifact_object_key == result.artifact_object_key


async def test_rerun_verifies_and_reuses_one_immutable_manifest(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    service = _service(context)
    first = await service.persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )
    second = await service.persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )

    assert first.reused is False
    assert second.reused is True
    assert second.artifact_id == first.artifact_id
    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 1


async def test_foreign_or_missing_source_artifact_is_not_readable(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    service = _service(context)

    with pytest.raises(ChunkManifestSourceNotFoundError):
        await service.persist(
            tenant_id=context.foreign_tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=context.result,
        )
    with pytest.raises(ChunkManifestSourceNotFoundError):
        await service.persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=uuid4(),
            config=context.config,
            result=context.result,
        )

    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 0


async def test_source_checksum_mismatch_is_rejected_before_object_write(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    mismatched = ChunkingResult(
        chunks=tuple(
            chunk.model_copy(update={"source_artifact_checksum": "b" * 64})
            for chunk in context.result.chunks
        )
    )

    with pytest.raises(ChunkManifestSourceMismatchError):
        await _service(context).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=mismatched,
        )
    assert not _has_chunk_manifest_object(context)


async def test_invalid_manifest_structure_is_rejected_before_object_write(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    child = next(chunk for chunk in context.result.chunks if chunk.chunk_kind == "child")
    invalid = ChunkingResult(
        chunks=context.result.chunks[: context.result.chunks.index(child)]
        + (child.model_copy(update={"parent_chunk_id": "chunk-" + "f" * 64}),)
        + context.result.chunks[context.result.chunks.index(child) + 1 :]
    )

    with pytest.raises(ChunkManifestInputError):
        await _service(context).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=invalid,
        )
    assert not _has_chunk_manifest_object(context)


async def test_storage_failure_does_not_create_metadata(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context

    class FailingWriteStorage:
        async def get(self, *, object_key: str) -> bytes:
            raise AssertionError(f"get should not be called: {object_key}")

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise ObjectStorageError("manifest unavailable")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError(f"delete should not be called: {object_key}")

    with pytest.raises(ChunkManifestStorageError):
        await ChunkManifestService(context.session, FailingWriteStorage()).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=context.result,
        )
    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 0


async def test_metadata_failure_cleans_the_new_manifest_object(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = chunk_artifact_context

    async def fail_create(*args: object, **kwargs: object) -> ChunkManifestArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.ChunkManifestArtifactRepository, "create", fail_create)

    with pytest.raises(ChunkManifestPersistenceError) as error:
        await _service(context).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=context.result,
        )

    assert error.value.cleanup_failed is False
    assert not _has_chunk_manifest_object(context)
    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 0


async def test_metadata_failure_reports_when_cleanup_cannot_remove_object(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = chunk_artifact_context

    class FailingCleanupStorage:
        async def get(self, *, object_key: str) -> bytes:
            return await context.storage.get(object_key=object_key)

        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            await context.storage.put(object_key=object_key, data=data, content_type=content_type)

        async def delete(self, *, object_key: str) -> None:
            raise ObjectStorageError("cleanup unavailable")

    async def fail_create(*args: object, **kwargs: object) -> ChunkManifestArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.ChunkManifestArtifactRepository, "create", fail_create)

    with pytest.raises(ChunkManifestPersistenceError) as error:
        await ChunkManifestService(context.session, FailingCleanupStorage()).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=context.result,
        )

    assert error.value.cleanup_failed is True
    assert _has_chunk_manifest_object(context)


async def test_corrupted_existing_manifest_is_not_reported_as_reusable(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    first = await _service(context).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )
    await context.storage.put(
        object_key=first.artifact_object_key,
        data=b"corrupted",
        content_type="application/octet-stream",
    )

    with pytest.raises(ChunkManifestConflictError):
        await _service(context).persist(
            tenant_id=context.tenant_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized_artifact_id,
            config=context.config,
            result=context.result,
        )

    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 1


async def test_changed_configuration_creates_a_new_manifest_identity(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    context = chunk_artifact_context
    first = await _service(context).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )
    changed_config = ChunkingConfig(
        parent_max_tokens=context.config.parent_max_tokens,
        parent_max_characters=context.config.parent_max_characters,
        child_max_tokens=context.config.child_max_tokens,
        child_max_characters=context.config.child_max_characters,
        child_overlap_tokens=0,
    )
    second = await _service(context).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=changed_config,
        result=context.result,
    )

    assert second.reused is False
    assert second.artifact_id != first.artifact_id
    assert second.chunking_config_checksum != first.chunking_config_checksum
    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 2


async def test_integrity_winner_is_verified_and_reused(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = chunk_artifact_context
    original_create = artifact_repository.ChunkManifestArtifactRepository.create

    async def create_then_report_conflict(
        repository: artifact_repository.ChunkManifestArtifactRepository,
        **kwargs: object,
    ) -> ChunkManifestArtifact:
        await original_create(repository, **kwargs)  # type: ignore[arg-type]
        await repository._session.commit()
        raise IntegrityError("simulated concurrent winner", {}, RuntimeError("winner"))

    monkeypatch.setattr(
        artifact_repository.ChunkManifestArtifactRepository,
        "create",
        create_then_report_conflict,
    )

    result = await _service(context).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )

    assert result.reused is True
    count = await context.session.scalar(select(func.count(ChunkManifestArtifact.id)))
    assert count == 1
