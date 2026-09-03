"""Proof for self-contained, tenant-scoped WikiRAG page artifacts."""

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
from openwikirag.application.wiki import WikiPage, WikiPageBuilder
from openwikirag.application.wiki_generation import (
    StructuredWikiGenerator,
    WikiGenerationRequest,
    WikiGenerationResult,
)
from openwikirag.application.wiki_generation_artifacts import (
    WikiGenerationArtifactService,
)
from openwikirag.application.wiki_page_artifacts import (
    WikiGeneratedPage,
    WikiPageArtifactConflictError,
    WikiPageArtifactInputError,
    WikiPageArtifactPersistenceError,
    WikiPageArtifactService,
    WikiPageArtifactSourceMismatchError,
    WikiPageArtifactSourceNotFoundError,
    WikiPageArtifactStorageError,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    Document,
    DocumentVersion,
    Tenant,
    WikiPageArtifact,
)
from openwikirag.infrastructure.repositories import wiki_page_artifacts as artifact_repository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorageError

CONFIG_HASH = "c" * 64
SOURCE_DATA = b"# Overview\nOpenWikiRAG uses citations.\n"


class StaticProvider:
    provider_identity = "fake-provider-v1"

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def generate(self, *, request: WikiGenerationRequest) -> object:
        return self.payload


class PageArtifactContext:
    def __init__(
        self,
        session: AsyncSession,
        storage: LocalObjectStorage,
        root: Path,
        tenant_id: UUID,
        foreign_tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        generation_artifact_id: UUID,
        page: WikiPage,
        generation: WikiGenerationResult,
    ) -> None:
        self.session = session
        self.storage = storage
        self.root = root
        self.tenant_id = tenant_id
        self.foreign_tenant_id = foreign_tenant_id
        self.document_version_id = document_version_id
        self.normalized_artifact_id = normalized_artifact_id
        self.generation_artifact_id = generation_artifact_id
        self.page = page
        self.generation = generation


@pytest.fixture
async def page_artifact_context(tmp_path: Path) -> AsyncIterator[PageArtifactContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wiki-page-artifact.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    root = tmp_path / "objects"
    storage = LocalObjectStorage(root)
    async with session_factory() as session:
        tenant = Tenant(name=f"Page Tenant {uuid4().hex}")
        foreign_tenant = Tenant(name=f"Foreign Tenant {uuid4().hex}")
        session.add_all((tenant, foreign_tenant))
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            title="Page Source",
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
        page, generation = _page_and_generation(document_model)
        persisted_generation = await WikiGenerationArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            result=generation,
        )
        yield PageArtifactContext(
            session=session,
            storage=storage,
            root=root,
            tenant_id=tenant.id,
            foreign_tenant_id=foreign_tenant.id,
            document_version_id=version.id,
            normalized_artifact_id=persisted_source.artifact_id,
            generation_artifact_id=persisted_generation.artifact_id,
            page=page,
            generation=generation,
        )
    await engine.dispose()


def _page_and_generation(document: NormalizedDocument) -> tuple[WikiPage, WikiGenerationResult]:
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
    generation = StructuredWikiGenerator(provider=StaticProvider(payload)).generate(
        document=document,
        page=page,
        config_hash=CONFIG_HASH,
    )
    return page, generation


def _service(context: PageArtifactContext) -> WikiPageArtifactService:
    return WikiPageArtifactService(context.session, context.storage)


async def _count_artifacts(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count(WikiPageArtifact.id)))
    assert count is not None
    return count


async def test_persist_stores_complete_page_package_outside_postgres(
    page_artifact_context: PageArtifactContext,
) -> None:
    persisted = await _service(page_artifact_context).persist(
        tenant_id=page_artifact_context.tenant_id,
        document_version_id=page_artifact_context.document_version_id,
        normalized_artifact_id=page_artifact_context.normalized_artifact_id,
        generation_artifact_id=page_artifact_context.generation_artifact_id,
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )

    package = WikiGeneratedPage(
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    assert persisted.reused is False
    assert persisted.page_checksum == page_artifact_context.page.checksum_sha256
    assert persisted.generation_result_checksum_sha256 == (
        page_artifact_context.generation.checksum_sha256
    )
    assert persisted.content_checksum_sha256 == package.checksum_sha256
    assert str(page_artifact_context.tenant_id) in persisted.artifact_object_key
    assert str(page_artifact_context.document_version_id) in persisted.artifact_object_key

    stored = json.loads(
        (
            await page_artifact_context.storage.get(
                object_key=persisted.artifact_object_key,
            )
        ).decode("utf-8")
    )
    assert stored == package.canonical_payload()
    assert stored["page"]["title"] == page_artifact_context.page.title
    assert stored["page"]["sections"] == json.loads(
        page_artifact_context.page.canonical_bytes()
    )["sections"]
    assert stored["generation"] == page_artifact_context.generation.canonical_payload()

    row = await page_artifact_context.session.get(WikiPageArtifact, persisted.artifact_id)
    assert row is not None
    assert row.tenant_id == page_artifact_context.tenant_id
    assert row.generation_artifact_id == page_artifact_context.generation_artifact_id
    assert row.content_checksum_sha256 == package.checksum_sha256
    assert row.review_status == "draft"
    assert await _count_artifacts(page_artifact_context.session) == 1


async def test_rerun_verifies_and_reuses_one_immutable_page_artifact(
    page_artifact_context: PageArtifactContext,
) -> None:
    service = _service(page_artifact_context)
    first = await service.persist(
        tenant_id=page_artifact_context.tenant_id,
        document_version_id=page_artifact_context.document_version_id,
        normalized_artifact_id=page_artifact_context.normalized_artifact_id,
        generation_artifact_id=page_artifact_context.generation_artifact_id,
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    second = await service.persist(
        tenant_id=page_artifact_context.tenant_id,
        document_version_id=page_artifact_context.document_version_id,
        normalized_artifact_id=page_artifact_context.normalized_artifact_id,
        generation_artifact_id=page_artifact_context.generation_artifact_id,
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )

    assert first.reused is False
    assert second.reused is True
    assert second.artifact_id == first.artifact_id
    assert await _count_artifacts(page_artifact_context.session) == 1


async def test_foreign_or_missing_generation_artifact_is_not_readable(
    page_artifact_context: PageArtifactContext,
) -> None:
    service = _service(page_artifact_context)

    with pytest.raises(WikiPageArtifactSourceNotFoundError):
        await service.persist(
            tenant_id=page_artifact_context.foreign_tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )
    with pytest.raises(WikiPageArtifactSourceNotFoundError):
        await service.persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=uuid4(),
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )
    assert await _count_artifacts(page_artifact_context.session) == 0


async def test_mismatched_page_is_rejected_before_object_write(
    page_artifact_context: PageArtifactContext,
) -> None:
    changed_page = page_artifact_context.page.model_copy(update={"title": "Different title"})

    with pytest.raises(WikiPageArtifactInputError):
        await _service(page_artifact_context).persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=changed_page,
            generation=page_artifact_context.generation,
        )
    assert await _count_artifacts(page_artifact_context.session) == 0
    package = WikiGeneratedPage(
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    object_key = (
        f"tenants/{page_artifact_context.tenant_id}/document-versions/"
        f"{page_artifact_context.document_version_id}/wiki-pages/"
        f"{page_artifact_context.page.checksum_sha256}/{package.checksum_sha256}.json"
    )
    assert not (page_artifact_context.root / object_key).exists()


async def test_generation_row_mismatch_is_rejected_before_object_write(
    page_artifact_context: PageArtifactContext,
) -> None:
    changed_generation = page_artifact_context.generation.model_copy(
        update={"provider_identity": "different-provider"}
    )

    with pytest.raises(WikiPageArtifactSourceMismatchError):
        await _service(page_artifact_context).persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=changed_generation,
        )
    assert await _count_artifacts(page_artifact_context.session) == 0
    assert not any(
        "wiki-pages" in path.as_posix() for path in page_artifact_context.root.rglob("*")
    )


async def test_storage_failure_does_not_create_page_metadata(
    page_artifact_context: PageArtifactContext,
) -> None:
    class FailingWriteStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            raise ObjectStorageError("object unavailable")

        async def get(self, *, object_key: str) -> bytes:
            raise AssertionError("get should not be called")

        async def delete(self, *, object_key: str) -> None:
            raise AssertionError("delete should not be called")

    with pytest.raises(WikiPageArtifactStorageError):
        await WikiPageArtifactService(
            page_artifact_context.session,
            FailingWriteStorage(),
        ).persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )
    assert await _count_artifacts(page_artifact_context.session) == 0


async def test_database_failure_cleans_new_page_object(
    page_artifact_context: PageArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_create(*args: object, **kwargs: object) -> WikiPageArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.WikiPageArtifactRepository, "create", fail_create)

    with pytest.raises(WikiPageArtifactPersistenceError) as error:
        await _service(page_artifact_context).persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )

    assert error.value.cleanup_failed is False
    package = WikiGeneratedPage(
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    object_key = (
        f"tenants/{page_artifact_context.tenant_id}/document-versions/"
        f"{page_artifact_context.document_version_id}/wiki-pages/"
        f"{page_artifact_context.page.checksum_sha256}/{package.checksum_sha256}.json"
    )
    assert not (page_artifact_context.root / object_key).exists()
    assert await _count_artifacts(page_artifact_context.session) == 0


async def test_cleanup_failure_is_reported_after_database_failure(
    page_artifact_context: PageArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCleanupStorage:
        async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
            await page_artifact_context.storage.put(
                object_key=object_key,
                data=data,
                content_type=content_type,
            )

        async def get(self, *, object_key: str) -> bytes:
            return await page_artifact_context.storage.get(object_key=object_key)

        async def delete(self, *, object_key: str) -> None:
            raise ObjectStorageError("cleanup unavailable")

    async def fail_create(*args: object, **kwargs: object) -> WikiPageArtifact:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artifact_repository.WikiPageArtifactRepository, "create", fail_create)

    with pytest.raises(WikiPageArtifactPersistenceError) as error:
        await WikiPageArtifactService(
            page_artifact_context.session,
            FailingCleanupStorage(),
        ).persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )

    assert error.value.cleanup_failed is True
    package = WikiGeneratedPage(
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    object_key = (
        f"tenants/{page_artifact_context.tenant_id}/document-versions/"
        f"{page_artifact_context.document_version_id}/wiki-pages/"
        f"{page_artifact_context.page.checksum_sha256}/{package.checksum_sha256}.json"
    )
    assert (page_artifact_context.root / object_key).exists()
    assert await _count_artifacts(page_artifact_context.session) == 0


async def test_corrupted_existing_page_object_is_not_reused(
    page_artifact_context: PageArtifactContext,
) -> None:
    service = _service(page_artifact_context)
    first = await service.persist(
        tenant_id=page_artifact_context.tenant_id,
        document_version_id=page_artifact_context.document_version_id,
        normalized_artifact_id=page_artifact_context.normalized_artifact_id,
        generation_artifact_id=page_artifact_context.generation_artifact_id,
        page=page_artifact_context.page,
        generation=page_artifact_context.generation,
    )
    await page_artifact_context.storage.put(
        object_key=first.artifact_object_key,
        data=b"corrupted",
        content_type="application/json",
    )

    with pytest.raises(WikiPageArtifactConflictError, match="checksum"):
        await service.persist(
            tenant_id=page_artifact_context.tenant_id,
            document_version_id=page_artifact_context.document_version_id,
            normalized_artifact_id=page_artifact_context.normalized_artifact_id,
            generation_artifact_id=page_artifact_context.generation_artifact_id,
            page=page_artifact_context.page,
            generation=page_artifact_context.generation,
        )
