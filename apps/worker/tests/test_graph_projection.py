"""Graph replay and opt-in real Neo4j lifecycle proof."""

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from apps.worker.tests.test_chunk_artifacts import ChunkArtifactContext
from apps.worker.tests.test_chunk_artifacts import chunk_artifact_context as chunk_artifact_context
from apps.worker.tests.test_knowledge import knowledge_context as knowledge_context
from openwikirag.application.graph_projection import (
    GraphNeighbor,
    GraphProjectionError,
    rebuild_projection,
)
from openwikirag.application.knowledge import KnowledgeArtifact, entity_id
from openwikirag.core.config import Settings
from openwikirag.infrastructure.models import KnowledgeArtifactRow
from openwikirag.infrastructure.neo4j import Neo4jProjection


class MemoryGraph:
    def __init__(self) -> None:
        self.artifacts: dict[UUID, KnowledgeArtifact] = {}

    async def upsert(self, artifact: KnowledgeArtifact) -> None:
        self.artifacts[artifact.id] = artifact

    async def neighbors(
        self,
        *,
        tenant_id: UUID,
        entity_id: str,
        limit: int = 20,
    ) -> tuple[GraphNeighbor, ...]:
        return tuple(
            GraphNeighbor(
                tenant_id=tenant_id,
                artifact_id=artifact.id,
                fact_id=fact.id,
                subject_id=fact.subject_id,
                object_id=fact.object_id,
            )
            for artifact in self.artifacts.values()
            if artifact.tenant_id == tenant_id
            for fact in artifact.facts
            if entity_id in (fact.subject_id, fact.object_id)
        )[:limit]


async def test_rebuild_tenant_filter_checksum_and_idempotence(
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
) -> None:
    ctx, artifact = knowledge_context
    graph = MemoryGraph()
    assert (
        await rebuild_projection(
            tenant_id=ctx.foreign_tenant_id, session=ctx.session, projection=graph
        )
        == 0
    )
    for _ in range(2):
        assert (
            await rebuild_projection(tenant_id=ctx.tenant_id, session=ctx.session, projection=graph)
            == 1
        )
    assert graph.artifacts == {artifact.id: artifact}
    row = await ctx.session.get(KnowledgeArtifactRow, artifact.id)
    assert row is not None
    row.checksum = "f" * 64
    await ctx.session.commit()
    with pytest.raises(GraphProjectionError, match="corrupt"):
        await rebuild_projection(tenant_id=ctx.tenant_id, session=ctx.session, projection=graph)


@pytest.fixture
async def real_graph() -> AsyncIterator[Neo4jProjection]:
    uri = os.getenv("OPENWIKIRAG_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set OPENWIKIRAG_TEST_NEO4J_URI for a disposable Neo4j integration instance.")
    graph = Neo4jProjection.from_settings(
        Settings(
            neo4j_uri=uri,
            neo4j_password=os.environ["OPENWIKIRAG_TEST_NEO4J_PASSWORD"],
        )
    )
    try:
        await graph.ensure_schema()
        yield graph
    finally:
        await graph.close()


async def test_real_neo4j_roundtrip_replay_isolation_conflict_clear_rebuild(
    real_graph: Neo4jProjection,
    knowledge_context: tuple[ChunkArtifactContext, KnowledgeArtifact],
) -> None:
    ctx, artifact = knowledge_context
    graph = real_graph
    try:
        for _ in range(2):
            await graph.upsert(artifact)
        neighbors = await graph.neighbors(
            tenant_id=ctx.tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora")
        )
        assert len(neighbors) == 2 and {item.artifact_id for item in neighbors} == {artifact.id}
        assert not await graph.neighbors(
            tenant_id=ctx.foreign_tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora")
        )
        assert (
            len(
                await graph.neighbors(
                    tenant_id=ctx.tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora"), limit=1
                )
            )
            == 1
        )
        with pytest.raises(GraphProjectionError):
            await graph.upsert(artifact.model_copy(update={"manifest_checksum": "f" * 64}))
        with pytest.raises(GraphProjectionError):
            await graph.clear(tenant_id=ctx.tenant_id, confirmed_tenant=uuid4())
        await graph.clear(tenant_id=ctx.foreign_tenant_id, confirmed_tenant=ctx.foreign_tenant_id)
        assert await graph.neighbors(
            tenant_id=ctx.tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora")
        )
        await graph.clear(tenant_id=ctx.tenant_id, confirmed_tenant=ctx.tenant_id)
        assert not await graph.neighbors(
            tenant_id=ctx.tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora")
        )
        assert (
            await rebuild_projection(tenant_id=ctx.tenant_id, session=ctx.session, projection=graph)
            == 1
        )
        assert (
            await graph.neighbors(
                tenant_id=ctx.tenant_id, entity_id=entity_id(ctx.tenant_id, "Aurora")
            )
            == neighbors
        )
    finally:
        await graph.clear(tenant_id=ctx.tenant_id, confirmed_tenant=ctx.tenant_id)


async def test_real_graph_invalid_controls_never_expand(real_graph: Neo4jProjection) -> None:
    for limit in (0, 51, True):
        with pytest.raises(GraphProjectionError, match="controls"):
            await real_graph.neighbors(tenant_id=uuid4(), entity_id="a" * 64, limit=limit)
