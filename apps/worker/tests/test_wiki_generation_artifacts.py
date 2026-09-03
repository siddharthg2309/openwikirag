"""Proof for immutable, tenant-scoped WikiRAG generation persistence."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.extraction import MarkdownExtractor, NormalizedDocument
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.normalized_artifacts import NormalizedArtifactService
from openwikirag.application.wiki import WikiPageBuilder
from openwikirag.application.wiki_generation import (
    GeneratedWikiContent,
    StructuredWikiGenerator,
    WikiGenerationRequest,
    WikiGenerationResult,
)
from openwikirag.application.wiki_generation_artifacts import (
    WikiGenerationArtifactConflictError,
    WikiGenerationArtifactPersistenceError,
    WikiGenerationArtifactService,
    WikiGenerationArtifactSourceMismatchError,
    WikiGenerationArtifactSourceNotFoundError,
    WikiGenerationArtifactStorageError,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    Document,
    DocumentVersion,
    Tenant,
    WikiGenerationArtifact,
)
from openwikirag.infrastructure.repositories import wiki_generation_artifacts as artifact_repository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError

CONFIG_HASH = "c" * 64
SOURCE_DATA = b"# Overview\nOpenWikiRAG uses citations.\n"


class StaticProvider:
    provider_identity = "fake-provider-v1"

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def generate(self, *, request: WikiGenerationRequest) -> object:
        return self.payload


class GenerationArtifactContext:
    def __init__(
        self,
        session: AsyncSession,
        storage: LocalObjectStorage,
        root: Path,
        tenant_id: UUID,
        foreign_tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        result: WikiGenerationResult,
    ) -> None:
        self.session = session
        self.storage = storage
        self.root = root
        self.tenant_id = tenant_id
        self.foreign_tenant_id = foreign_tenant_id
        self.document_version_id = document_version_id
        self.normalized_artifact_id = normalized_artifact_id
        self.result = result


@pytest.fixture
async def generation_context(tmp_path: Path) -> AsyncIterator[GenerationArtifactContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wiki-generation.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    root = tmp_path / "objects"
    storage = LocalObjectStorage(root)
    async with session_factory() as session:
        tenant = Tenant(name=f"Generation Tenant {uuid4().hex}")
        foreign_tenant = Tenant(name=f"Foreign Tenant {uuid4().hex}")
        session.add_all((tenant, foreign_tenant))
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            title="Generation Source",
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
        document_model = MarkdownExtractor().extract(SOURCE_DATA)
        result = _generation_result(document_model)
        yield GenerationArtifactContext(
            session=session,
            storage=storage,
            root=root,
            tenant_id=tenant.id,
            foreign_tenant_id=foreign_tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            result=result,
        )
    await engine.dispose()


def _generation_result(document: NormalizedDocument) -> WikiGenerationResult:
    metadata = DeterministicMetadataExtractor().extract(document=document)
    page = WikiPageBuilder().build(document=document, metadata=metadata)
    value = "OpenWikiRAG uses citations."
    start = document.text.index(value)
    span = next(
        span
        for span in document.spans
        if span.normalized_start_char <= start
        and start + len(value) <= span.normalized_end_char
    )
    evidence = {
        "value": value,
        "raw_text": value,
        "normalized_start_char": start,
        "normalized_end_char": start + len(value),
        "section_path": list(span.section_path),
        "page_number": span.page_number,
    }
    payload = {
        "summary": {
            "text": "The document describes citation-backed knowledge.",
            "evidence": [evidence],
        },
        "definitions": [],
        "references": [],
    }
    return StructuredWikiGenerator(provider=StaticProvider(payload)).generate(
        document=document,
        page=page,
        config_hash=CONFIG_HASH,
    )


def _service(context: GenerationArtifactContext) -> WikiGenerationArtifactService:
    return WikiGenerationArtifactService(context.session, context.storage)


async def _count_artifacts(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count(WikiGenerationArtifact.id)))
    assert count is not None
    return count


async def test_persist_stores_canonical_json_outside_postgres(
    generation_context: GenerationArtifactContext,
) -> None:
    persisted = await _service(generation_context).persist(
        tenant_id=generation_context.tenant_id,
        document_version_id=generation_context.document_version_id,
        normalized_artifact_id=generation_context.normalized_artifact_id,
        result=generation_context.result,
    )

    assert persisted.reused is False
    assert persisted.base_page_checksum == generation_context.result.base_page_checksum
    assert persisted.source_artifact_checksum == generation_context.result.source_artifact_checksum
    assert str(generation_context.tenant_id) in persisted.artifact_object_key
    assert str(generation_context.document_version_id) in persisted.artifact_object_key
    stored = json.loads(
        (
            await generation_context.storage.get(
                object_key=persisted.artifact_object_key,
            )
        ).decode("utf-8")
    )
    assert stored == generation_context.result.canonical_payload()

    row = await generation_context.session.get(WikiGenerationArtifact, persisted.artifact_id)
    assert row is not None
    assert row.tenant_id == generation_context.tenant_id
    assert row.document_version_id == generation_context.document_version_id
    assert row.normalized_artifact_id == generation_context.normalized_artifact_id
    assert row.result_checksum_sha256 == generation_context.result.checksum_sha256
    assert row.review_status == "draft"
    assert await _count_artifacts(generation_context.session) == 1


async def test_rerun_verifies_and_reuses_one_immutable_artifact(
    generation_context: GenerationArtifactContext,
) -> None:
    service = _service(generation_context)
    first = await service.persist(
        tenant_id=generation_context.tenant_id,
        document_version_id=generation_context.document_version_id,
        normalized_artifact_id=generation_context.normalized_artifact_id,
        result=generation_context.result,
    )
    second = await service.persist(
        tenant_id=generation_context.tenant_id,
        document_version_id=generation_context.document_version_id,
        normalized_artifact_id=generation_context.normalized_artifact_id,
        result=generation_context.result,
    )

    assert first.reused is False
    assert second.reused is True
    assert second.artifact_id == first.artifact_id
    assert await _count_artifacts(generation_context.session) == 1


async def test_foreign_or_missing_source_artifact_is_not_readable(
    generation_context: GenerationArtifactContext,
) -> None:
    service = _service(generation_context)

    with pytest.raises(WikiGenerationArtifactSourceNotFoundError):
        await service.persist(
            tenant_id=generation_context.foreign_tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=generation_context.result,
        )
    with pytest.raises(WikiGenerationArtifactSourceNotFoundError):
        await service.persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=uuid4(),
            result=generation_context.result,
        )
    assert await _count_artifacts(generation_context.session) == 0


async def test_source_checksum_mismatch_fails_before_object_write(
    generation_context: GenerationArtifactContext,
) -> None:
    result = generation_context.result.model_copy(update={"source_artifact_checksum": "d" * 64})

    with pytest.raises(WikiGenerationArtifactSourceMismatchError):
        await _service(generation_context).persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=result,
        )
    assert await _count_artifacts(generation_context.session) == 0
    assert not any(
        "wiki-generation" in path.as_posix()
        for path in generation_context.root.rglob("*")
    )


async def test_storage_failure_does_not_create_metadata(
    generation_context: GenerationArtifactContext,
) -> None:
    class FailingWriteStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise ObjectStorageError("object unavailable")

        async def get(self, *, object_key: str) -> bytes:
            raise AssertionError("get should not be called")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    with pytest.raises(WikiGenerationArtifactStorageError):
        await WikiGenerationArtifactService(
            generation_context.session,
            FailingWriteStorage(),
        ).persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=generation_context.result,
        )
    assert await _count_artifacts(generation_context.session) == 0


async def test_database_failure_cleans_new_object(
    generation_context: GenerationArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_create(*args: object, **kwargs: object) -> WikiGenerationArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.WikiGenerationArtifactRepository, "create", fail_create)

    with pytest.raises(WikiGenerationArtifactPersistenceError) as error:
        await _service(generation_context).persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=generation_context.result,
        )

    assert error.value.cleanup_failed is False
    object_key = (
        f"tenants/{generation_context.tenant_id}/document-versions/"
        f"{generation_context.document_version_id}/wiki-generation/"
        f"{generation_context.result.base_page_checksum}/{generation_context.result.checksum_sha256}.json"
    )
    assert not (generation_context.root / object_key).exists()
    assert await _count_artifacts(generation_context.session) == 0


async def test_cleanup_failure_is_reported_after_database_failure(
    generation_context: GenerationArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCleanupStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            await generation_context.storage.put(
                object_key=object_key,
                data=data,
                content_type=content_type,
            )

        async def get(self, *, object_key: str) -> bytes:
            return await generation_context.storage.get(object_key=object_key)

        async def delete(self, *, object_key: str) -> None:
            raise ObjectStorageError("cleanup unavailable")

    async def fail_create(*args: object, **kwargs: object) -> WikiGenerationArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.WikiGenerationArtifactRepository, "create", fail_create)

    with pytest.raises(WikiGenerationArtifactPersistenceError) as error:
        await WikiGenerationArtifactService(
            generation_context.session,
            FailingCleanupStorage(),
        ).persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=generation_context.result,
        )

    assert error.value.cleanup_failed is True
    assert any("wiki-generation" in path.as_posix() for path in generation_context.root.rglob("*"))
    assert await _count_artifacts(generation_context.session) == 0


async def test_changed_content_same_identity_is_rejected_without_overwrite(
    generation_context: GenerationArtifactContext,
) -> None:
    service = _service(generation_context)
    first = await service.persist(
        tenant_id=generation_context.tenant_id,
        document_version_id=generation_context.document_version_id,
        normalized_artifact_id=generation_context.normalized_artifact_id,
        result=generation_context.result,
    )
    changed = generation_context.result.model_copy(
        update={"content": GeneratedWikiContent(summary=None)},
    )

    with pytest.raises(WikiGenerationArtifactConflictError):
        await service.persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=changed,
        )
    stored = await generation_context.storage.get(object_key=first.artifact_object_key)
    assert stored == generation_context.result.canonical_bytes()
    assert await _count_artifacts(generation_context.session) == 1


async def test_corrupted_existing_object_is_not_reported_as_reusable(
    generation_context: GenerationArtifactContext,
) -> None:
    service = _service(generation_context)
    first = await service.persist(
        tenant_id=generation_context.tenant_id,
        document_version_id=generation_context.document_version_id,
        normalized_artifact_id=generation_context.normalized_artifact_id,
        result=generation_context.result,
    )
    await generation_context.storage.put(
        object_key=first.artifact_object_key,
        data=b"corrupted",
        content_type="application/json",
    )

    with pytest.raises(WikiGenerationArtifactConflictError, match="checksum"):
        await service.persist(
            tenant_id=generation_context.tenant_id,
            document_version_id=generation_context.document_version_id,
            normalized_artifact_id=generation_context.normalized_artifact_id,
            result=generation_context.result,
        )
