"""Canonical graph artifact proof; explicit annotations, not inferred NLP facts."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from apps.worker.tests.test_chunk_artifacts import ChunkArtifactContext
from apps.worker.tests.test_chunk_artifacts import chunk_artifact_context as chunk_artifact_context
from apps.worker.tests.test_evidence import _persist
from openwikirag.application.chunking import HierarchicalChunker
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.knowledge import (
    KnowledgeArtifact,
    KnowledgeArtifactService,
    KnowledgeError,
    entity_id,
    normalize_entity,
)
from openwikirag.infrastructure.models import KnowledgeArtifactRow, NormalizedDocumentArtifact

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
