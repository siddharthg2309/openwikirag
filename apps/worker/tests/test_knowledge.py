"""Canonical graph artifact proof; explicit and conservative prose facts."""

import hashlib
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import func, select

from apps.worker.tests.test_chunk_artifacts import ChunkArtifactContext
from apps.worker.tests.test_chunk_artifacts import chunk_artifact_context as chunk_artifact_context
from apps.worker.tests.test_evidence import _persist
from openwikirag.application.chunk_artifacts import ChunkManifestService
from openwikirag.application.chunking import HierarchicalChunker
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.knowledge import (
    LEGACY_EXTRACTOR,
    KnowledgeArtifact,
    KnowledgeArtifactService,
    KnowledgeError,
    entity_id,
    normalize_entity,
)
from openwikirag.application.normalized_artifacts import NormalizedArtifactService
from openwikirag.infrastructure.models import (
    Document,
    DocumentVersion,
    KnowledgeArtifactRow,
    NormalizedDocumentArtifact,
)

GRAPH_TEXT = (
    b"# Architecture\n[Aurora] --calls--> [Borealis]\n"
    b"[Borealis] --depends_on--> [Cygnus]\n[AURORA] --uses--> [Cache]\n"
)


@pytest.fixture
async def knowledge_context(
    chunk_artifact_context: ChunkArtifactContext,
) -> AsyncIterator[tuple[ChunkArtifactContext, KnowledgeArtifact]]:
    ctx = chunk_artifact_context
    # Prepare this isolated fixture's source before persisting its first manifest.
    document = MarkdownExtractor().extract(GRAPH_TEXT)
    source = await ctx.session.get(NormalizedDocumentArtifact, ctx.normalized_artifact_id)
    assert source is not None
    source.content_checksum_sha256 = document.checksum_sha256
    ctx.result = HierarchicalChunker(ctx.config).chunk(document)
    await ctx.session.commit()
    saved, _ = await _persist(ctx)
    artifact = await KnowledgeArtifactService(ctx.session, ctx.storage).build(
        tenant_id=ctx.tenant_id,
        manifest_id=saved.artifact_id,
    )
    await ctx.session.commit()
    yield ctx, artifact


def test_entity_identity_normalizes_case_width_space_but_not_tenants() -> None:
    tenant = uuid4()
    assert normalize_entity("  ＡＵＲＯＲＡ ") == "aurora"
    assert entity_id(tenant, "Aurora") == entity_id(tenant, "ＡＵＲＯＲＡ")
    assert entity_id(tenant, "Aurora") != entity_id(uuid4(), "Aurora")
    for value in ("", "a\x00b", "x" * 121):
        with pytest.raises(ValueError):
            normalize_entity(value)


async def test_canonical_graph_exact_quotes_idempotence_and_schema(
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
) -> None:
    ctx, artifact = knowledge_context
    assert len(artifact.entities) == 4 and len(artifact.facts) == 3
    for fact in artifact.facts:
        chunk = next(chunk for chunk in ctx.result.chunks if chunk.chunk_id == fact.chunk_id)
        start = fact.start_char - chunk.normalized_start_char
        assert chunk.text[start : start + len(fact.quote)] == fact.quote
    assert artifact == await KnowledgeArtifactService(ctx.session, ctx.storage).build(
        tenant_id=ctx.tenant_id,
        manifest_id=artifact.manifest_id,
    )
    assert await ctx.session.scalar(select(func.count()).select_from(KnowledgeArtifactRow)) == 1
    invalid = artifact.model_dump()
    invalid["facts"][0]["predicate"] = "owned_by"
    with pytest.raises(ValueError):
        KnowledgeArtifact.model_validate(invalid)


async def test_foreign_source_never_reads_objects(
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, artifact = knowledge_context
    getter = AsyncMock(wraps=ctx.storage.get)
    monkeypatch.setattr(ctx.storage, "get", getter)
    with pytest.raises(KnowledgeError, match="unavailable"):
        await KnowledgeArtifactService(ctx.session, ctx.storage).build(
            tenant_id=ctx.foreign_tenant_id,
            manifest_id=artifact.manifest_id,
        )
    getter.assert_not_awaited()


async def test_corrupt_manifest_and_mutated_artifact_fail_closed(
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, artifact = knowledge_context
    service = KnowledgeArtifactService(ctx.session, ctx.storage)
    with monkeypatch.context() as patch:
        patch.setattr(ctx.storage, "get", AsyncMock(return_value=b"corrupted"))
        with pytest.raises(KnowledgeError, match="checksum"):
            await service.build(tenant_id=ctx.tenant_id, manifest_id=artifact.manifest_id)
    row = await ctx.session.get(KnowledgeArtifactRow, artifact.id)
    assert row is not None
    row.checksum = "f" * 64
    await ctx.session.commit()
    with pytest.raises(KnowledgeError, match="failed"):
        await service.build(tenant_id=ctx.tenant_id, manifest_id=artifact.manifest_id)


async def test_conservative_free_text_relations_preserve_exact_evidence(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    ctx = chunk_artifact_context
    source_data = (
        b"# Relations\n"
        b"Aurora uses Redis.\n"
        b"\xef\xbc\xa1urora depends on Redis.\n"
        b"Borealis is owned by Platform Team.\n"
        b"Aurora uses Redis and Borealis calls Cygnus.\n"
        b"The service has a dependency on Redis.\n"
    )
    document = Document(tenant_id=ctx.tenant_id, title="Natural Relations", source_type="markdown")
    ctx.session.add(document)
    await ctx.session.flush()
    source_key = f"tenants/{ctx.tenant_id}/documents/{document.id}/natural.md"
    version = DocumentVersion(
        tenant_id=ctx.tenant_id,
        document_id=document.id,
        version_number=1,
        original_filename="natural.md",
        sanitized_filename="natural.md",
        source_type="markdown",
        media_type="text/markdown",
        byte_size=len(source_data),
        checksum_sha256="c" * 64,
        source_object_key=source_key,
    )
    ctx.session.add(version)
    await ctx.session.flush()
    document.current_version_id = version.id
    await ctx.storage.put(object_key=source_key, data=source_data, content_type="text/markdown")
    await ctx.session.commit()

    normalized = await NormalizedArtifactService(ctx.session, ctx.storage).persist(
        tenant_id=ctx.tenant_id,
        document_version_id=version.id,
    )
    result = HierarchicalChunker(ctx.config).chunk(document=normalized.normalized_document)
    manifest = await ChunkManifestService(ctx.session, ctx.storage).persist(
        tenant_id=ctx.tenant_id,
        document_version_id=version.id,
        normalized_artifact_id=normalized.artifact_id,
        config=ctx.config,
        result=result,
    )
    artifact = await KnowledgeArtifactService(ctx.session, ctx.storage).build(
        tenant_id=ctx.tenant_id,
        manifest_id=manifest.artifact_id,
    )

    assert artifact.extractor == "relations-v2"
    assert len(artifact.facts) == 3
    assert {item.name for item in artifact.entities} == {
        "aurora",
        "redis",
        "borealis",
        "platform team",
    }
    assert {fact.predicate for fact in artifact.facts} == {"uses", "depends_on", "owned_by"}
    for fact in artifact.facts:
        assert normalized.normalized_document.text[fact.start_char : fact.end_char] == fact.quote
        assert fact.quote.endswith(".")


async def test_legacy_explicit_relation_artifact_remains_readable(
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
) -> None:
    ctx, artifact = knowledge_context
    legacy_id = uuid5(artifact.manifest_id, LEGACY_EXTRACTOR)
    legacy_facts = tuple(
        fact.model_copy(
            update={
                "id": hashlib.sha256(
                    f"{legacy_id}:{fact.chunk_id}:{fact.start_char}:{fact.quote}".encode()
                ).hexdigest()
            }
        )
        for fact in artifact.facts
    )
    legacy = artifact.model_copy(
        update={"id": legacy_id, "extractor": LEGACY_EXTRACTOR, "facts": legacy_facts}
    )
    assert KnowledgeArtifact.model_validate(legacy.model_dump()) == legacy
