"""Inspectable, restartable answer stages; checkpoint state is never authority."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field

from openwikirag.application.answers import (
    AnswerContext,
    GenerationInputError,
    GroundedAnswer,
    GroundedGenerationService,
    pack_context,
)
from openwikirag.application.deduplication import EvidenceGroup, deduplicate_evidence
from openwikirag.application.evidence import EvidenceIntegrityError, EvidenceNotFoundError
from openwikirag.application.fusion import ReciprocalRankFusionService
from openwikirag.application.graph_projection import GraphProjectionError
from openwikirag.application.reranking import RerankingProviderError
from openwikirag.application.retrieval import CandidateRetrievalResult, SearchFilters, SearchRequest
from openwikirag.application.search import SearchService
from openwikirag.application.source_evidence import SourceEvidence, SourceEvidenceResolver
from openwikirag.security.authorization import AuthorizationService, Permission, Principal


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str = Field(min_length=1, max_length=4096)
    mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    candidate_limit: int = Field(default=20, ge=1, le=100, strict=True)
    limit: int = Field(default=8, ge=1, le=8, strict=True)
    rerank: bool = Field(default=False, strict=True)
    graph_hops: int = Field(default=0, ge=0, le=2, strict=True)
    filters: SearchFilters = Field(default_factory=SearchFilters)

    def search(self, tenant_id: UUID) -> SearchRequest:
        return SearchRequest(
            tenant_id=tenant_id,
            query=self.query,
            mode=self.mode,
            candidate_limit=self.candidate_limit,
            filters=self.filters,
        )


class AnswerState(TypedDict, total=False):
    tenant: str
    user: str
    provider: str
    request: dict[str, Any]
    candidates: dict[str, Any] | None
    groups: list[dict[str, Any]] | None
    sources: list[dict[str, Any]] | None
    context: dict[str, Any] | None
    answer: dict[str, Any] | None
    trace: list[str]


class AnswerWorkflow:
    stages = (
        "authorize",
        "retrieve",
        "fuse",
        "resolve",
        "graph",
        "rerank",
        "context",
        "generate",
        "validate",
    )

    def __init__(
        self,
        *,
        principal: Principal,
        search: SearchService,
        resolver: Callable[[], SourceEvidenceResolver],
        generation: GroundedGenerationService,
        checkpointer: BaseCheckpointSaver[Any],
        reauthorize: Callable[[], Awaitable[None]] | None = None,
    ):
        self.principal, self.search, self.resolver, self.generation = (
            principal,
            search,
            resolver,
            generation,
        )
        self.reauthorize = reauthorize
        self.tenant_id = UUID(principal.tenant_id)
        self.user_id = UUID(principal.subject_id)
        builder = StateGraph(AnswerState)
        previous = START
        for name in self.stages:
            builder.add_node(name, RunnableLambda(self._node(name)))
            builder.add_edge(previous, name)
            previous = name
        builder.add_edge(previous, END)
        self.graph = builder.compile(checkpointer=checkpointer)

    def config(self, run_id: UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": f"ow:{self.tenant_id}:{self.user_id}:{run_id}"}}

    async def _guard(self, state: AnswerState) -> None:
        AuthorizationService().require(self.principal, Permission.READ_DOCUMENTS)
        if self.reauthorize is not None:
            await self.reauthorize()
        if (
            state.get("tenant") != str(self.tenant_id)
            or state.get("user") != str(self.user_id)
            or state.get("provider") != self.generation.provider.identity
        ):
            raise GenerationInputError("Checkpoint owner or provider differs.")
        if len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode()) > 1024 * 1024:
            raise GenerationInputError("Checkpoint exceeds the 1 MiB state budget.")

    def _node(self, name: str) -> Callable[[AnswerState], Awaitable[AnswerState]]:
        async def run(state: AnswerState) -> AnswerState:
            await self._guard(state)
            update = await self._stage(name, state)
            update["trace"] = [*state.get("trace", []), name]
            await self._guard({**state, **update})
            return update

        return run

    async def _fresh(self, sources: tuple[SourceEvidence, ...]) -> None:
        resolver = self.resolver()
        for source in sources:
            fresh = await resolver.resolve(
                tenant_id=self.tenant_id,
                manifest_id=source.manifest_id,
                chunk_id=source.chunk.chunk_id,
            )
            if fresh != source:
                raise EvidenceIntegrityError(
                    "Checkpoint evidence differs from current canonical source."
                )

    async def validate_answer(self, answer: GroundedAnswer) -> None:
        """Recheck stored or cached answers before releasing them to a caller."""
        resolver = self.resolver()
        answer = GroundedAnswer.model_validate(answer.model_dump())
        for citation in answer.citations:
            fresh = await resolver.resolve(
                tenant_id=self.tenant_id,
                manifest_id=citation.manifest_id,
                chunk_id=citation.chunk_id,
            )
            offset = citation.start_char - fresh.chunk.normalized_start_char
            if (
                fresh.identity != citation.evidence_id
                or fresh.document_id != citation.document_id
                or fresh.document_version_id != citation.document_version_id
                or offset < 0
                or fresh.chunk.text[offset : offset + len(citation.quote)] != citation.quote
                or (fresh.chunk.page_start, fresh.chunk.page_end)
                != (citation.page_start, citation.page_end)
            ):
                raise EvidenceIntegrityError(
                    "Answer citation no longer matches its canonical source."
                )

    async def _stage(self, name: str, state: AnswerState) -> AnswerState:
        request = AnswerRequest.model_validate(state["request"])
        spec = request.search(self.tenant_id)
        sources = tuple(SourceEvidence.model_validate(item) for item in state.get("sources") or [])
        if name == "authorize":
            if request.graph_hops and (
                self.search.graph is None or spec.filters != SearchFilters()
            ):
                raise GenerationInputError(
                    "Graph answers require configured, unfiltered graph retrieval."
                )
            if request.rerank and self.search.reranker is None:
                raise RerankingProviderError("No reranker configured.")
            return {}
        if name == "retrieve":
            result = await self.search.retrieval.retrieve(spec)
            return {"candidates": result.model_dump(mode="json")}
        if name == "fuse":
            candidates = CandidateRetrievalResult.model_validate(state["candidates"])
            fused = ReciprocalRankFusionService().fuse(candidates)
            groups = deduplicate_evidence(fused).groups
            return {"groups": [item.model_dump(mode="json") for item in groups], "candidates": None}
        if name == "resolve":
            found: list[SourceEvidence] = []
            for raw in state.get("groups") or []:
                group = EvidenceGroup.model_validate(raw)
                try:
                    item = await self.search.resolver.resolve(
                        tenant_id=self.tenant_id,
                        payload=group.representative.payload,
                        require_ready=True,
                    )
                except EvidenceNotFoundError:
                    continue
                found.append(SourceEvidence.from_retrieval(item))
                if len(found) >= (20 if request.rerank else request.limit):
                    break
            return {"sources": [item.model_dump(mode="json") for item in found], "groups": None}
        if name == "graph":
            if request.graph_hops:
                if self.search.graph is None:
                    raise GraphProjectionError("Graph is unavailable.")
                await self._fresh(sources)
                expansion = await self.search.graph.expand(
                    principal=self.principal, seeds=sources, hops=request.graph_hops
                )
                merged = {item.identity: item for item in sources}
                for neighbor in expansion.evidence:
                    merged.setdefault(neighbor.evidence.identity, neighbor.evidence)
                return {"sources": [item.model_dump(mode="json") for item in merged.values()]}
            return {}
        if name == "rerank":
            if request.rerank:
                if self.search.reranker is None:
                    raise RerankingProviderError("Reranker is unavailable.")
                await self._fresh(sources)
                sources = await self.search.reranker.rank_sources(
                    tenant_id=self.tenant_id,
                    query=spec.normalized_query,
                    evidence=sources,
                    limit=request.limit,
                )
            return {"sources": [item.model_dump(mode="json") for item in sources]}
        if name == "context":
            context = pack_context(
                tenant_id=self.tenant_id, query=spec.normalized_query, evidence=sources
            )
            return {"context": context.model_dump(mode="json"), "sources": None}
        if name == "generate":
            context = AnswerContext.model_validate(state["context"])
            await self._fresh(tuple(item.source for item in context.passages))
            answer = await self.generation.generate(context)
            return {"answer": answer.model_dump(mode="json")}
        if name == "validate":
            await self.validate_answer(GroundedAnswer.model_validate(state["answer"]))
            return {"context": None}
        raise GenerationInputError("Unknown workflow stage.")

    async def events(
        self, run_id: UUID, request: AnswerRequest | None = None
    ) -> AsyncIterator[str]:
        # Do not let a developer's global tracing environment export source text.
        with tracing_context(enabled=False, parent=False):
            async for stage in self._events(run_id, request):
                yield stage

    async def _events(
        self, run_id: UUID, request: AnswerRequest | None = None
    ) -> AsyncIterator[str]:
        config = self.config(run_id)
        snapshot = await self.graph.aget_state(config)
        if request is None:
            if not snapshot.values:
                raise GenerationInputError("No checkpoint exists for this owner and run.")
            await self._guard(cast(AnswerState, snapshot.values))
            initial = None
        else:
            if snapshot.values:
                raise GenerationInputError("Run already exists; resume it instead.")
            initial = AnswerState(
                tenant=str(self.tenant_id),
                user=str(self.user_id),
                provider=self.generation.provider.identity,
                request=request.model_dump(mode="json"),
                trace=[],
            )
            await self._guard(initial)
        async for update in self.graph.astream(
            initial, config, stream_mode="updates", durability="sync"
        ):
            for stage in update:
                if stage in self.stages:
                    yield stage

    async def result(self, run_id: UUID) -> GroundedAnswer:
        snapshot = await self.graph.aget_state(self.config(run_id))
        await self._guard(cast(AnswerState, snapshot.values))
        if snapshot.next or "validate" not in snapshot.values.get("trace", []):
            raise GenerationInputError("Answer workflow has not completed.")
        answer = GroundedAnswer.model_validate(snapshot.values.get("answer"))
        await self.validate_answer(answer)
        return answer

    async def execute(self, run_id: UUID, request: AnswerRequest | None = None) -> GroundedAnswer:
        async for _ in self.events(run_id, request):
            pass
        return await self.result(run_id)
