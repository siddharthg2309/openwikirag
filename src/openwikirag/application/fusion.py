"""Deterministic, explainable reciprocal-rank fusion for retrieval candidates."""

import math
from dataclasses import dataclass
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openwikirag.application.retrieval import (
    MAX_QUERY_CHARACTERS,
    MAX_RETRIEVAL_CANDIDATES,
    CandidateRetrievalResult,
    RetrievalLeg,
    RetrievalMode,
    SearchCandidate,
)
from openwikirag.application.vector_index import VectorPointPayload

DEFAULT_RRF_K = 60
MAX_RRF_K = 1_000
MAX_FUSED_CANDIDATES = MAX_RETRIEVAL_CANDIDATES
_LEG_ORDER: dict[RetrievalLeg, int] = {"dense": 0, "sparse": 1}


class FusionError(Exception):
    """Base error for candidate-fusion failures."""


class FusionInputError(FusionError):
    """Raised when fusion input or configuration is malformed."""


class FusionIntegrityError(FusionError):
    """Raised when immutable candidate identity or generated output conflicts."""


@dataclass(frozen=True, slots=True)
class RrfFusionConfig:
    """Server-owned bounds for one reciprocal-rank fusion operation."""

    rrf_k: int = DEFAULT_RRF_K
    fused_limit: int = 20

    def __post_init__(self) -> None:
        if type(self.rrf_k) is not int or not 1 <= self.rrf_k <= MAX_RRF_K:
            raise FusionInputError(f"RRF k must be between 1 and {MAX_RRF_K}.")
        if (
            type(self.fused_limit) is not int
            or not 1 <= self.fused_limit <= MAX_FUSED_CANDIDATES
        ):
            raise FusionInputError(
                f"Fused limit must be between 1 and {MAX_FUSED_CANDIDATES}."
            )


class FusionContribution(BaseModel):
    """Auditable contribution from one independently ranked retrieval leg."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    leg: RetrievalLeg
    source_rank: int = Field(ge=1, le=MAX_RETRIEVAL_CANDIDATES)
    source_score: float
    reciprocal_rank_score: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_scores(self) -> Self:
        if not math.isfinite(self.source_score):
            raise ValueError("Fusion source scores must be finite.")
        if not math.isfinite(self.reciprocal_rank_score):
            raise ValueError("Fusion reciprocal-rank scores must be finite.")
        return self


class FusedCandidate(BaseModel):
    """One deduplicated point with its complete rank-fusion explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    rank: int = Field(ge=1, le=MAX_FUSED_CANDIDATES)
    fused_score: float = Field(gt=0.0)
    best_source_rank: int = Field(ge=1, le=MAX_RETRIEVAL_CANDIDATES)
    contributions: tuple[FusionContribution, ...] = Field(min_length=1, max_length=2)
    payload: VectorPointPayload

    @model_validator(mode="after")
    def validate_explanation(self) -> Self:
        if not math.isfinite(self.fused_score):
            raise ValueError("Fused scores must be finite.")
        legs = tuple(contribution.leg for contribution in self.contributions)
        if len(legs) != len(set(legs)):
            raise ValueError("A fused candidate cannot repeat a retrieval leg.")
        if legs != tuple(sorted(legs, key=_LEG_ORDER.__getitem__)):
            raise ValueError("Fusion contributions must use stable leg order.")
        expected_best_rank = min(
            contribution.source_rank for contribution in self.contributions
        )
        if self.best_source_rank != expected_best_rank:
            raise ValueError("Best source rank does not match the contributions.")
        expected_score = math.fsum(
            contribution.reciprocal_rank_score
            for contribution in self.contributions
        )
        if not math.isclose(
            self.fused_score,
            expected_score,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("Fused score does not match its contributions.")
        return self


class ReciprocalRankFusionResult(BaseModel):
    """One deterministic fused ordering with query and configuration lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    normalized_query: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)
    query_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: RetrievalMode
    source_candidate_limit: int = Field(ge=1, le=MAX_RETRIEVAL_CANDIDATES)
    rrf_k: int = Field(ge=1, le=MAX_RRF_K)
    fused_limit: int = Field(ge=1, le=MAX_FUSED_CANDIDATES)
    candidates: tuple[FusedCandidate, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len(self.candidates) > self.fused_limit:
            raise ValueError("Fusion returned more candidates than its limit.")
        if tuple(candidate.rank for candidate in self.candidates) != tuple(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("Fused candidate ranks must be contiguous.")
        point_ids = tuple(candidate.point_id for candidate in self.candidates)
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("Fused candidates cannot contain duplicate points.")
        if any(
            candidate.payload.tenant_id != self.tenant_id
            for candidate in self.candidates
        ):
            raise ValueError("A fused candidate crossed the tenant boundary.")
        expected_order = tuple(
            sorted(
                self.candidates,
                key=lambda candidate: (
                    -candidate.fused_score,
                    candidate.best_source_rank,
                    candidate.point_id,
                ),
            )
        )
        if self.candidates != expected_order:
            raise ValueError("Fused candidates are not deterministically ordered.")
        for candidate in self.candidates:
            for contribution in candidate.contributions:
                if contribution.source_rank > self.source_candidate_limit:
                    raise ValueError("A contribution exceeds the source candidate limit.")
                expected_contribution = 1.0 / (
                    self.rrf_k + contribution.source_rank
                )
                if not math.isclose(
                    contribution.reciprocal_rank_score,
                    expected_contribution,
                    rel_tol=1e-12,
                    abs_tol=1e-15,
                ):
                    raise ValueError("A contribution does not match the configured RRF k.")
                if self.mode == "dense" and contribution.leg != "dense":
                    raise ValueError("Dense mode cannot contain sparse contributions.")
                if self.mode == "sparse" and contribution.leg != "sparse":
                    raise ValueError("Sparse mode cannot contain dense contributions.")
        return self


@dataclass(slots=True)
class _CandidateAccumulator:
    payload: VectorPointPayload
    contributions: list[FusionContribution]


class ReciprocalRankFusionService:
    """Fuse dense and sparse ranks without comparing their raw score scales."""

    def __init__(self, config: RrfFusionConfig | None = None) -> None:
        if config is not None and not isinstance(config, RrfFusionConfig):
            raise FusionInputError("RRF requires an RrfFusionConfig.")
        self._config = config or RrfFusionConfig()

    @property
    def config(self) -> RrfFusionConfig:
        """Return the immutable server-owned fusion configuration."""

        return self._config

    def fuse(self, retrieval: CandidateRetrievalResult) -> ReciprocalRankFusionResult:
        """Deduplicate point identities and sum auditable reciprocal ranks."""

        validated = _validate_retrieval_result(retrieval)
        accumulators: dict[str, _CandidateAccumulator] = {}
        self._accumulate_leg(
            accumulators,
            candidates=validated.dense_candidates,
            leg="dense",
        )
        self._accumulate_leg(
            accumulators,
            candidates=validated.sparse_candidates,
            leg="sparse",
        )

        staged: list[
            tuple[str, float, int, tuple[FusionContribution, ...], VectorPointPayload]
        ] = []
        for point_id, accumulator in accumulators.items():
            contributions = tuple(
                sorted(
                    accumulator.contributions,
                    key=lambda contribution: _LEG_ORDER[contribution.leg],
                )
            )
            fused_score = math.fsum(
                contribution.reciprocal_rank_score
                for contribution in contributions
            )
            best_source_rank = min(
                contribution.source_rank for contribution in contributions
            )
            staged.append(
                (
                    point_id,
                    fused_score,
                    best_source_rank,
                    contributions,
                    accumulator.payload,
                )
            )

        ordered = sorted(
            staged,
            key=lambda item: (-item[1], item[2], item[0]),
        )[: self._config.fused_limit]
        try:
            fused_candidates = tuple(
                FusedCandidate(
                    point_id=point_id,
                    rank=rank,
                    fused_score=fused_score,
                    best_source_rank=best_source_rank,
                    contributions=contributions,
                    payload=payload,
                )
                for rank, (
                    point_id,
                    fused_score,
                    best_source_rank,
                    contributions,
                    payload,
                ) in enumerate(ordered, start=1)
            )
            return ReciprocalRankFusionResult(
                tenant_id=validated.tenant_id,
                normalized_query=validated.normalized_query,
                query_checksum_sha256=validated.query_checksum_sha256,
                mode=validated.mode,
                source_candidate_limit=validated.candidate_limit,
                rrf_k=self._config.rrf_k,
                fused_limit=self._config.fused_limit,
                candidates=fused_candidates,
            )
        except ValidationError as exc:
            raise FusionIntegrityError(
                "RRF could not produce a valid fused result."
            ) from exc

    def _accumulate_leg(
        self,
        accumulators: dict[str, _CandidateAccumulator],
        *,
        candidates: tuple[SearchCandidate, ...],
        leg: RetrievalLeg,
    ) -> None:
        for candidate in candidates:
            contribution = FusionContribution(
                leg=leg,
                source_rank=candidate.rank,
                source_score=candidate.score,
                reciprocal_rank_score=1.0 / (self._config.rrf_k + candidate.rank),
            )
            accumulator = accumulators.get(candidate.point_id)
            if accumulator is None:
                accumulators[candidate.point_id] = _CandidateAccumulator(
                    payload=candidate.payload,
                    contributions=[contribution],
                )
                continue
            if accumulator.payload != candidate.payload:
                raise FusionIntegrityError(
                    "The same point id has conflicting cross-leg provenance."
                )
            accumulator.contributions.append(contribution)


def _validate_retrieval_result(
    retrieval: CandidateRetrievalResult,
) -> CandidateRetrievalResult:
    if not isinstance(retrieval, CandidateRetrievalResult):
        raise FusionInputError("RRF requires a CandidateRetrievalResult.")
    try:
        return CandidateRetrievalResult.model_validate(
            retrieval.model_dump(mode="python")
        )
    except ValidationError as exc:
        raise FusionInputError("The candidate retrieval result is invalid.") from exc


__all__ = [
    "DEFAULT_RRF_K",
    "FusedCandidate",
    "FusionContribution",
    "FusionError",
    "FusionInputError",
    "FusionIntegrityError",
    "MAX_FUSED_CANDIDATES",
    "MAX_RRF_K",
    "ReciprocalRankFusionResult",
    "ReciprocalRankFusionService",
    "RrfFusionConfig",
]
