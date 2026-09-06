"""Graph expansion over real canonical documents and optional live Neo4j."""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_ingestion import (
    FakeTransport,
    create_job_with_source,
    make_message,
    make_service,
)
from apps.api.tests.test_ingestion import session as session
from apps.worker.tests.test_graph_projection import MemoryGraph
from apps.worker.tests.test_graph_projection import real_graph as real_graph
from openwikirag.application.evidence import CanonicalEvidenceResolver, EvidenceIntegrityError
from openwikirag.application.graph_projection import GraphProjectionError
from openwikirag.application.graph_retrieval import GraphExpansionService
from openwikirag.application.knowledge import KnowledgeArtifact
from openwikirag.application.retrieval import (
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    SearchFilters,
    SearchRequest,
)
from openwikirag.application.search import SearchService
from openwikirag.application.vector_index import InMemoryVectorIndex
from openwikirag.application.wiki_ingestion import WikiIngestionHandler
from openwikirag.infrastructure.models import Document, DocumentVersion, KnowledgeArtifactRow
from openwikirag.infrastructure.neo4j import Neo4jProjection
from openwikirag.infrastructure.storage import LocalObjectStorage
from openwikirag.security.authorization import Principal, Role


class GraphContext:
    def __init__(
        self,
        session: AsyncSession,
        storage: LocalObjectStorage,
        graph: MemoryGraph,
        index: InMemoryVectorIndex,
        tenant_id: UUID,
        versions: list[UUID],
    ) -> None:
        self.session, self.storage, self.graph, self.index = session, storage, graph, index
        self.tenant_id, self.versions = tenant_id, versions
        self.principal = Principal(str(uuid4()), str(tenant_id), Role.VIEWER)

    def service(self) -> SearchService:
        return SearchService(
            CandidateRetrievalService(InMemoryCandidateIndex(self.index.points)),
            CanonicalEvidenceResolver(self.session, self.storage),
            graph=GraphExpansionService(self.session, self.storage, self.graph),
        )


@pytest.fixture
async def graph_context(session: AsyncSession, tmp_path: Path) -> AsyncIterator[GraphContext]:
    storage, graph, index = (
        LocalObjectStorage(tmp_path / "objects"),
        MemoryGraph(),
        InMemoryVectorIndex(),
    )
    tenant_id, versions = None, []
    for text in (
        "[Aurora] --calls--> [Borealis]",
        "[Borealis] --depends_on--> [Cygnus]",
        "[Cygnus] --uses--> [Delta]",
    ):
        tenant, job = await create_job_with_source(
            session, storage, source_type="markdown", data=text.encode(), tenant_id=tenant_id
        )
        tenant_id = tenant.id
        versions.append(job.document_version_id)
        transport = FakeTransport()
        transport.new_messages.append(make_message(tenant_id, job.id))
        handler = WikiIngestionHandler(session, storage, index, config_hash="b" * 64, graph=graph)
        assert await make_service(session, transport, handler).consume_once() == 1
        await session.refresh(job)
        assert job.status == "succeeded"
    assert tenant_id is not None
    yield GraphContext(session, storage, graph, index, tenant_id, versions)


async def test_graph_adds_connected_passages_missing_from_initial_retrieval(
    graph_context: GraphContext,
) -> None:
    ctx = graph_context
    request = SearchRequest(tenant_id=ctx.tenant_id, query="Aurora", mode="sparse")
    initial = await ctx.service().search(principal=ctx.principal, request=request, limit=1)
    dense = await ctx.service().search(
        principal=ctx.principal,
        limit=1,
        request=SearchRequest(tenant_id=ctx.tenant_id, query="Aurora", mode="dense"),
    )
    one = await ctx.service().search(
        principal=ctx.principal, request=request, limit=1, graph_hops=1
    )
    two = await ctx.service().search(
        principal=ctx.principal, request=request, limit=1, graph_hops=2
    )
    assert len(initial.evidence) == 1
    assert ctx.versions[1] not in {item.document_version_id for item in initial.evidence}
    assert ctx.versions[1] in {item.document_version_id for item in one.evidence}
    assert ctx.versions[2] not in {item.document_version_id for item in one.evidence}
    assert ctx.versions[2] in {item.document_version_id for item in two.evidence}
    assert len({item.identity for item in two.evidence}) == len(two.evidence)
    assert two.graph is not None and two.graph.visited_count <= 20
    assert {item.document_version_id for item in two.evidence} - {
        item.document_version_id for item in dense.evidence
    }


async def test_stale_intermediate_fact_cannot_extend_the_frontier(
    graph_context: GraphContext,
) -> None:
    ctx = graph_context
    version = await ctx.session.get(DocumentVersion, ctx.versions[1])
    assert version is not None
    document = await ctx.session.get(Document, version.document_id)
    assert document is not None
    document.current_version_id = None
    await ctx.session.commit()
    result = await ctx.service().search(
        principal=ctx.principal,
        request=SearchRequest(tenant_id=ctx.tenant_id, query="Aurora", mode="sparse"),
        limit=1,
        graph_hops=2,
    )
    assert ctx.versions[1] not in {item.document_version_id for item in result.evidence}
    assert ctx.versions[2] not in {item.document_version_id for item in result.evidence}
    assert result.graph is not None and result.graph.stale_count > 0


async def test_foreign_graph_ref_and_corrupt_canonical_artifact_fail_closed(
    graph_context: GraphContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = graph_context
    request = SearchRequest(tenant_id=ctx.tenant_id, query="Aurora", mode="sparse")
    artifact = next(iter(ctx.graph.artifacts.values()))
    neighbors = await ctx.graph.neighbors(
        tenant_id=ctx.tenant_id, entity_id=artifact.facts[0].subject_id
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            ctx.graph,
            "neighbors",
            AsyncMock(return_value=(neighbors[0].model_copy(update={"tenant_id": uuid4()}),)),
        )
        with pytest.raises(GraphProjectionError, match="foreign"):
            await ctx.service().search(
                principal=ctx.principal, request=request, limit=1, graph_hops=2
            )
    row = await ctx.session.scalar(
        select(KnowledgeArtifactRow).where(
            KnowledgeArtifactRow.document_version_id == ctx.versions[0]
        )
    )
    assert row is not None
    row.checksum = "f" * 64
    await ctx.session.commit()
    with pytest.raises(EvidenceIntegrityError):
        await ctx.service().search(principal=ctx.principal, request=request, limit=1, graph_hops=2)


async def test_live_neo4j_expansion_uses_canonical_connected_sources(
    graph_context: GraphContext,
    real_graph: Neo4jProjection,
) -> None:
    ctx = graph_context
    try:
        for artifact in ctx.graph.artifacts.values():
            await real_graph.upsert(KnowledgeArtifact.model_validate(artifact.model_dump()))
        service = ctx.service()
        service.graph = GraphExpansionService(ctx.session, ctx.storage, real_graph)
        result = await service.search(
            principal=ctx.principal,
            request=SearchRequest(tenant_id=ctx.tenant_id, query="Aurora", mode="sparse"),
            limit=1,
            graph_hops=2,
        )
        assert ctx.versions[2] in {item.document_version_id for item in result.evidence}
    finally:
        await real_graph.clear(tenant_id=ctx.tenant_id, confirmed_tenant=ctx.tenant_id)


async def test_graph_filters_apply_before_frontier_expansion(
    graph_context: GraphContext,
) -> None:
    ctx = graph_context
    result = await ctx.service().search(
        principal=ctx.principal,
        graph_hops=2,
        request=SearchRequest(
            tenant_id=ctx.tenant_id,
            query="Aurora",
            mode="sparse",
            filters=SearchFilters(document_version_ids=(ctx.versions[0], ctx.versions[1])),
        ),
    )
    evidence_versions = {item.document_version_id for item in result.evidence}
    assert ctx.versions[1] in evidence_versions
    assert ctx.versions[2] not in evidence_versions

    language_result = await ctx.service().search(
        principal=ctx.principal,
        graph_hops=1,
        request=SearchRequest(
            tenant_id=ctx.tenant_id,
            query="Aurora",
            mode="sparse",
            filters=SearchFilters(languages=("und",)),
        ),
    )
    assert language_result.graph is not None

    with pytest.raises(GraphProjectionError):
        await GraphExpansionService(ctx.session, ctx.storage, ctx.graph).expand(
            principal=ctx.principal, seeds=(), hops=3
        )


async def test_graph_projection_failure_rolls_back_artifact_without_ack(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    tenant, job = await create_job_with_source(
        session, storage, source_type="markdown", data=b"[Aurora] --calls--> [Borealis]"
    )
    graph = MemoryGraph()
    monkeypatch.setattr(graph, "upsert", AsyncMock(side_effect=GraphProjectionError("outage")))
    transport = FakeTransport()
    transport.new_messages.append(make_message(tenant.id, job.id))
    handler = WikiIngestionHandler(
        session, storage, InMemoryVectorIndex(), config_hash="b" * 64, graph=graph
    )
    assert await make_service(session, transport, handler).consume_once() == 1
    await session.refresh(job)
    assert job.status == "retryable" and transport.acknowledged == []
    assert not tuple(await session.scalars(select(KnowledgeArtifactRow)))
