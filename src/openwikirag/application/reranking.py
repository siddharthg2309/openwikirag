"""Bounded tenant-safe pairwise reranking over canonical text."""

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from openwikirag.application.deduplication import evidence_identity
from openwikirag.application.evidence import ResolvedEvidence
from openwikirag.application.retrieval import RetrievalInputError, SearchRequest


class RerankingError(Exception):
    """The reranker could not safely return an ordered evidence set."""


class RerankingInputError(RerankingError):
    """Invalid, foreign, duplicate, or oversized evidence input."""


class RerankingProviderError(RerankingError):
    """Provider unavailable, busy, timed out, or returned invalid scores."""


class PairwiseReranker(Protocol):
    @property
    def identity(self) -> str: ...

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class RerankingConfig:
    max_candidates: int = 40
    max_passage_characters: int = 8_000
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            type(self.max_candidates) is not int
            or not 1 <= self.max_candidates <= 100
            or type(self.max_passage_characters) is not int
            or not 1 <= self.max_passage_characters <= 32_000
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 120
        ):
            raise RerankingInputError("Invalid reranking limits.")


class RankedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    rank: int = Field(ge=1, le=100)
    source_rank: int = Field(ge=1, le=100)
    score: float
    evidence: ResolvedEvidence


class RerankingService:
    def __init__(self, provider: PairwiseReranker, config: RerankingConfig | None = None) -> None:
        self.provider = provider
        self.config = config or RerankingConfig()
        if not isinstance(provider.identity, str) or not provider.identity.strip():
            raise RerankingInputError("A reranker must have a reproducible identity.")

    async def rerank(
        self,
        *,
        tenant_id: UUID,
        query: str,
        evidence: tuple[ResolvedEvidence, ...],
        limit: int = 10,
    ) -> tuple[RankedEvidence, ...]:
        if (
            type(limit) is not int
            or not 1 <= limit <= self.config.max_candidates
            or len(evidence) > self.config.max_candidates
        ):
            raise RerankingInputError("Reranking candidate or result limit is invalid.")
        try:
            normalized = SearchRequest(tenant_id=tenant_id, query=query).normalized_query
            evidence = tuple(
                ResolvedEvidence.model_validate(item.model_dump()) for item in evidence
            )
        except (ValueError, AttributeError, RetrievalInputError) as exc:
            raise RerankingInputError("Invalid canonical evidence.") from exc
        identities = [evidence_identity(item.payload) for item in evidence]
        if (
            len(set(identities)) != len(identities)
            or any(item.payload.tenant_id != tenant_id for item in evidence)
            or any(len(item.chunk.text) > self.config.max_passage_characters for item in evidence)
        ):
            raise RerankingInputError("Foreign, duplicate, or oversized evidence.")
        if not evidence:
            return ()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                scores = await self.provider.score(
                    tuple((normalized, item.chunk.text) for item in evidence)
                )
            if len(scores) != len(evidence) or any(
                type(score) not in (float, int) or not math.isfinite(score) for score in scores
            ):
                raise ValueError("Expected one finite numeric score per candidate.")
        except Exception as exc:
            raise RerankingProviderError("The reranking provider failed.") from exc
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:limit]
        return tuple(
            RankedEvidence(
                rank=rank, source_rank=index + 1, score=scores[index], evidence=evidence[index]
            )
            for rank, index in enumerate(order, start=1)
        )
