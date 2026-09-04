"""Real stages, persisted recovery, stale source rejection and private run scope."""

from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import AsyncConnection
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_postgres_integration import postgres_session as postgres_session
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from apps.worker.tests.test_graph_retrieval import GraphContext
from apps.worker.tests.test_graph_retrieval import graph_context as graph_context
from apps.worker.tests.test_graph_retrieval import session as session
from openwikirag.application.answers import (
    AnswerContext,
    DraftAnswer,
    DraftClaim,
    GenerationInputError,
    GenerationRetryableError,
    GroundedGenerationService,
)
from openwikirag.application.evidence import EvidenceNotFoundError
from openwikirag.application.reranking import RerankingProviderError, RerankingService
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.workflow import AnswerRequest, AnswerWorkflow
from openwikirag.infrastructure.checkpoints import checkpoint_store, setup_checkpoints
from openwikirag.infrastructure.models import Document, DocumentVersion
from openwikirag.security.authorization import Principal, Role


class ExtractiveProvider:
    identity = "extractive-workflow-fixture-v1"

    def __init__(self, *, fail: bool = False):
        self.fail, self.calls = fail, 0

    async def generate(self, context: AnswerContext) -> DraftAnswer:
        self.calls += 1
        if self.fail:
            raise GenerationRetryableError("simulated timeout")
        return DraftAnswer(
            status="answered",
            claims=(
                DraftClaim(
                    evidence_id="E1",
                    quote=context.passages[0].excerpt.strip(),
                ),
            ),
        )


def workflow(
    ctx: GraphContext, saver: BaseCheckpointSaver[Any], provider: ExtractiveProvider | None = None
) -> AnswerWorkflow:
    return AnswerWorkflow(
        principal=ctx.principal,
        search=ctx.service(),
        resolver=lambda: SourceEvidenceResolver(ctx.session, ctx.storage),
        generation=GroundedGenerationService(provider or ExtractiveProvider(), max_attempts=1),
        checkpointer=saver,
    )


async def test_inspectable_workflow_and_owner_scope(graph_context: GraphContext) -> None:
    ctx, saver, run_id = graph_context, InMemorySaver(), uuid4()
    service = workflow(ctx, saver)
    events = [
        name
        async for name in service.events(
            run_id, AnswerRequest(query="Aurora", mode="sparse", graph_hops=2)
        )
    ]
    assert events == list(service.stages)
    answer = await service.result(run_id)
    assert answer.status == "answered" and answer.citations
    snapshot = await service.graph.aget_state(service.config(run_id))
    assert snapshot.values["context"] is None
    assert snapshot.values["sources"] is None
    assert (
        len([item async for item in service.graph.aget_state_history(service.config(run_id))]) >= 10
    )
    with pytest.raises(GenerationInputError, match="already exists"):
        await service.execute(run_id, AnswerRequest(query="different"))
    ctx.principal = Principal(str(uuid4()), str(ctx.tenant_id), Role.ADMIN)
    with pytest.raises(GenerationInputError, match="No checkpoint"):
        await workflow(ctx, saver).execute(run_id)


async def test_failed_generation_resumes_without_retrieving_again(
    graph_context: GraphContext,
) -> None:
    ctx, saver, run_id = graph_context, InMemorySaver(), uuid4()
    failed = workflow(ctx, saver, ExtractiveProvider(fail=True))
    with pytest.raises(GenerationRetryableError):
        await failed.execute(run_id, AnswerRequest(query="Aurora", mode="sparse"))
    snapshot = await failed.graph.aget_state(failed.config(run_id))
    assert snapshot.next == ("generate",) and snapshot.values["context"]
    resumed = workflow(ctx, saver)
    assert [name async for name in resumed.events(run_id)] == ["generate", "validate"]
    assert (await resumed.result(run_id)).status == "answered"


async def test_source_invalidated_during_model_call_blocks_publication(
    graph_context: GraphContext,
) -> None:
    ctx = graph_context

    class Invalidating(ExtractiveProvider):
        async def generate(self, context: AnswerContext) -> DraftAnswer:
            version = await ctx.session.get(
                DocumentVersion, context.passages[0].source.document_version_id
            )
            assert version is not None
            document = await ctx.session.get(Document, version.document_id)
            assert document is not None
            document.current_version_id = None
            await ctx.session.commit()
            return await super().generate(context)

    service = workflow(ctx, InMemorySaver(), Invalidating())
    run_id = uuid4()
    with pytest.raises(EvidenceNotFoundError):
        await service.execute(run_id, AnswerRequest(query="Aurora", mode="sparse"))
    snapshot = await service.graph.aget_state(service.config(run_id))
    assert snapshot.next == ("validate",)
    with pytest.raises(GenerationInputError, match="not completed"):
        await service.result(run_id)


async def test_real_postgres_fresh_connection_recovers_checkpoint(
    postgres_url: str, postgres_session: AsyncSession, graph_context: GraphContext
) -> None:
    await setup_checkpoints(postgres_url, grant_role="openwikirag_rls_test")
    run_id = uuid4()
    async with checkpoint_store(postgres_url) as saver:
        first = workflow(graph_context, saver, ExtractiveProvider(fail=True))
        with pytest.raises(GenerationRetryableError):
            await first.execute(run_id, AnswerRequest(query="Aurora", mode="sparse"))
    # A new connection, graph instance and provider simulate the stopped process.
    async with checkpoint_store(postgres_url) as saver:
        assert isinstance(saver.conn, AsyncConnection)
        await saver.conn.execute("SET ROLE openwikirag_rls_test")
        second = workflow(graph_context, saver)
        assert (await second.execute(run_id)).status == "answered"
        snapshot = await second.graph.aget_state(second.config(run_id))
        assert snapshot.values["trace"] == list(second.stages)
        await saver.adelete_thread(second.config(run_id)["configurable"]["thread_id"])
        assert not (await second.graph.aget_state(second.config(run_id))).values


async def test_checkpoint_provider_change_and_foreign_tenant_rejected(
    graph_context: GraphContext,
) -> None:
    ctx, saver, run_id = graph_context, InMemorySaver(), uuid4()
    first = workflow(ctx, saver, ExtractiveProvider(fail=True))
    with pytest.raises(GenerationRetryableError):
        await first.execute(run_id, AnswerRequest(query="Aurora", mode="sparse"))
    provider = ExtractiveProvider()
    provider.identity = "different-provider"
    with pytest.raises(GenerationInputError, match="provider differs"):
        await workflow(ctx, saver, provider).execute(run_id)
    ctx.principal = Principal(ctx.principal.subject_id, str(uuid4()), Role.OPERATOR)
    with pytest.raises(GenerationInputError, match="No checkpoint"):
        await workflow(ctx, saver).execute(run_id)


async def test_graph_sources_reach_bounded_reranker(graph_context: GraphContext) -> None:
    class Scorer:
        identity = "test-finite-source-scores"
        calls = 0
        invalid = False

        async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
            self.calls += 1
            assert len(pairs) >= 3
            return tuple(float("nan") if self.invalid else float(i) for i in range(len(pairs)))

    scorer = Scorer()
    first = workflow(graph_context, InMemorySaver())
    first.search.reranker = RerankingService(scorer)
    result = await first.execute(
        uuid4(), AnswerRequest(query="Aurora", mode="sparse", graph_hops=2, rerank=True)
    )
    assert scorer.calls == 1 and result.citations
    scorer.invalid = True
    second = workflow(graph_context, InMemorySaver())
    second.search.reranker = RerankingService(scorer)
    with pytest.raises(RerankingProviderError):
        await second.execute(
            uuid4(), AnswerRequest(query="Aurora", mode="sparse", graph_hops=2, rerank=True)
        )
