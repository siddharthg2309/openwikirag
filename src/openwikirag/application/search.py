"""Authorized search composition; projection hits become verified source passages."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from openwikirag.application.deduplication import EvidenceGroup, deduplicate_evidence
from openwikirag.application.evidence import (
    CanonicalEvidenceResolver,
    EvidenceNotFoundError,
    ResolvedEvidence,
)
from openwikirag.application.fusion import ReciprocalRankFusionService, RrfFusionConfig
from openwikirag.application.reranking import RerankingProviderError, RerankingService
from openwikirag.application.retrieval import (
    CandidateRetrievalService,
    RetrievalInputError,
    SearchRequest,
)
from openwikirag.security.authorization import AuthorizationService, Permission, Principal


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rank: int = Field(ge=1, le=40)
    evidence: ResolvedEvidence
    explanation: EvidenceGroup
    rerank_score: float | None = Field(default=None, allow_inf_nan=False)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query_checksum: str
    hits: tuple[SearchHit, ...]
    candidate_count: int
    duplicate_count: int
    unavailable_count: int
    reranker_identity: str | None = None


class SearchService:
    def __init__(
        self,
        retrieval: CandidateRetrievalService,
        resolver: CanonicalEvidenceResolver,
        reranker: RerankingService | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.resolver = resolver
        self.reranker = reranker

    async def search(
        self,
        *,
        principal: Principal,
        request: SearchRequest,
        limit: int = 10,
        rerank: bool = False,
    ) -> SearchResult:
        AuthorizationService().require(
            principal,
            Permission.READ_DOCUMENTS,
            resource_tenant_id=str(request.tenant_id),
        )
        # Even a platform operator must explicitly authenticate in the target tenant.
        if UUID(principal.tenant_id) != request.tenant_id:
            raise RetrievalInputError("Search must use the authenticated tenant.")
        if type(limit) is not int or not 1 <= limit <= 40 or type(rerank) is not bool:
            raise RetrievalInputError("Invalid search result controls.")
        if rerank and self.reranker is None:
            raise RerankingProviderError("No reranker is configured.")
        candidates = await self.retrieval.retrieve(request)
        fused = ReciprocalRankFusionService(RrfFusionConfig(fused_limit=100)).fuse(candidates)
        grouped = deduplicate_evidence(fused)
        resolved: list[tuple[ResolvedEvidence, EvidenceGroup]] = []
        unavailable = 0
        window = self.reranker.config.max_candidates if rerank and self.reranker else limit
        for group in grouped.groups:
            try:
                evidence = await self.resolver.resolve(
                    tenant_id=request.tenant_id,
                    payload=group.representative.payload,
                    require_ready=True,
                )
            except EvidenceNotFoundError:
                unavailable += 1
                continue
            resolved.append((evidence, group))
            if len(resolved) >= window:
                break
        identity = None
        if rerank and self.reranker is not None:
            ranked = await self.reranker.rerank(
                tenant_id=request.tenant_id,
                query=request.normalized_query,
                evidence=tuple(item[0] for item in resolved),
                limit=limit,
            )
            hits = tuple(
                SearchHit(
                    rank=item.rank,
                    evidence=item.evidence,
                    explanation=resolved[item.source_rank - 1][1],
                    rerank_score=item.score,
                )
                for item in ranked
            )
            identity = self.reranker.provider.identity
        else:
            hits = tuple(
                SearchHit(rank=index, evidence=evidence, explanation=group)
                for index, (evidence, group) in enumerate(resolved, 1)
            )
        return SearchResult(
            query_checksum=request.query_checksum_sha256,
            hits=hits,
            candidate_count=len(candidates.dense_candidates) + len(candidates.sparse_candidates),
            duplicate_count=grouped.duplicate_count,
            unavailable_count=unavailable,
            reranker_identity=identity,
        )
