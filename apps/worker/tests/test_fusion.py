"""Proof for deterministic explainable reciprocal-rank fusion."""

from collections.abc import Callable
from uuid import UUID

import pytest
from pydantic import ValidationError

from apps.worker.tests.test_vector_index import FOREIGN_TENANT_ID, TENANT_ID, _request
from openwikirag.application.fusion import (
    FusedCandidate,
    FusionContribution,
    FusionInputError,
    FusionIntegrityError,
    ReciprocalRankFusionResult,
    ReciprocalRankFusionService,
    RrfFusionConfig,
)
from openwikirag.application.retrieval import (
    CandidateRetrievalResult,
    RetrievalLeg,
    RetrievalMode,
    SearchCandidate,
    SearchRequest,
)
from openwikirag.application.vector_index import VectorPoint

VERSION_B = UUID("55555555-5555-5555-5555-555555555555")
VERSION_C = UUID("66666666-6666-6666-6666-666666666666")
QUERY = SearchRequest(tenant_id=TENANT_ID, query="hybrid retrieval evidence")


async def _points() -> tuple[VectorPoint, VectorPoint, VectorPoint]:
    first = (await _request(text="First evidence passage.")).build_point()
    second = (
        await _request(
            document_version_id=VERSION_B,
            text="Second evidence passage.",
        )
    ).build_point()
    third = (
        await _request(
            document_version_id=VERSION_C,
            text="Third evidence passage.",
        )
    ).build_point()
    return first, second, third


def _candidate(
    point: VectorPoint,
    *,
    leg: RetrievalLeg,
    rank: int,
    score: float,
) -> SearchCandidate:
    return SearchCandidate(
        point_id=point.point_id,
        leg=leg,
        rank=rank,
        score=score,
        payload=point.payload,
    )


def _result(
    *,
    mode: RetrievalMode = "hybrid",
    dense: tuple[SearchCandidate, ...] = (),
    sparse: tuple[SearchCandidate, ...] = (),
    candidate_limit: int = 10,
) -> CandidateRetrievalResult:
    return CandidateRetrievalResult(
        tenant_id=TENANT_ID,
        normalized_query=QUERY.normalized_query,
        query_checksum_sha256=QUERY.query_checksum_sha256,
        mode=mode,
        candidate_limit=candidate_limit,
        dense_candidates=dense,
        sparse_candidates=sparse,
    )


async def test_rrf_sums_both_legs_deduplicates_and_explains_formula() -> None:
    first, second, third = await _points()
    retrieval = _result(
        dense=(
            _candidate(first, leg="dense", rank=1, score=0.81),
            _candidate(second, leg="dense", rank=2, score=0.79),
        ),
        sparse=(
            _candidate(third, leg="sparse", rank=1, score=18.0),
            _candidate(first, leg="sparse", rank=2, score=4.0),
        ),
    )

    fused = ReciprocalRankFusionService().fuse(retrieval)

    assert tuple(candidate.point_id for candidate in fused.candidates) == (
        first.point_id,
        third.point_id,
        second.point_id,
    )
    assert len(fused.candidates) == 3
    shared = fused.candidates[0]
    assert shared.fused_score == pytest.approx((1 / 61) + (1 / 62))
    assert shared.best_source_rank == 1
    assert tuple(item.leg for item in shared.contributions) == ("dense", "sparse")
    assert tuple(item.source_rank for item in shared.contributions) == (1, 2)
    assert tuple(item.source_score for item in shared.contributions) == (0.81, 4.0)
    assert shared.contributions[0].reciprocal_rank_score == pytest.approx(1 / 61)
    assert shared.contributions[1].reciprocal_rank_score == pytest.approx(1 / 62)
    assert fused.tenant_id == retrieval.tenant_id
    assert fused.query_checksum_sha256 == retrieval.query_checksum_sha256


async def test_rrf_uses_ranks_not_raw_scores_and_breaks_ties_by_point_id() -> None:
    first, second, _third = await _points()
    retrieval = _result(
        dense=(_candidate(first, leg="dense", rank=1, score=-1_000_000.0),),
        sparse=(_candidate(second, leg="sparse", rank=1, score=1_000_000.0),),
    )

    fused = ReciprocalRankFusionService().fuse(retrieval)

    assert fused.candidates[0].fused_score == pytest.approx(
        fused.candidates[1].fused_score
    )
    assert tuple(candidate.point_id for candidate in fused.candidates) == tuple(
        sorted((first.point_id, second.point_id))
    )


@pytest.mark.parametrize("mode,leg", [("dense", "dense"), ("sparse", "sparse")])
async def test_single_leg_modes_preserve_rank_order(
    mode: RetrievalMode,
    leg: RetrievalLeg,
) -> None:
    first, second, _third = await _points()
    candidates = (
        _candidate(first, leg=leg, rank=1, score=0.5),
        _candidate(second, leg=leg, rank=2, score=0.9),
    )
    retrieval = _result(
        mode=mode,
        dense=candidates if leg == "dense" else (),
        sparse=candidates if leg == "sparse" else (),
    )

    fused = ReciprocalRankFusionService().fuse(retrieval)

    assert tuple(candidate.point_id for candidate in fused.candidates) == (
        first.point_id,
        second.point_id,
    )
    assert all(
        tuple(contribution.leg for contribution in candidate.contributions) == (leg,)
        for candidate in fused.candidates
    )


async def test_fused_limit_applies_after_cross_leg_deduplication() -> None:
    first, second, third = await _points()
    retrieval = _result(
        dense=(
            _candidate(first, leg="dense", rank=1, score=0.8),
            _candidate(second, leg="dense", rank=2, score=0.7),
            _candidate(third, leg="dense", rank=3, score=0.6),
        ),
        sparse=(
            _candidate(first, leg="sparse", rank=1, score=8.0),
            _candidate(second, leg="sparse", rank=2, score=7.0),
            _candidate(third, leg="sparse", rank=3, score=6.0),
        ),
    )

    fused = ReciprocalRankFusionService(
        RrfFusionConfig(rrf_k=10, fused_limit=2)
    ).fuse(retrieval)

    assert tuple(candidate.point_id for candidate in fused.candidates) == (
        first.point_id,
        second.point_id,
    )
    assert tuple(candidate.rank for candidate in fused.candidates) == (1, 2)
    assert fused.rrf_k == 10
    assert fused.fused_limit == 2


def test_empty_candidate_lists_return_valid_empty_fusion() -> None:
    fused = ReciprocalRankFusionService().fuse(_result())

    assert fused.candidates == ()
    assert fused.mode == "hybrid"
    assert fused.source_candidate_limit == 10


async def test_same_point_with_conflicting_cross_leg_payload_fails_closed() -> None:
    first, second, _third = await _points()
    dense = _candidate(first, leg="dense", rank=1, score=0.8)
    conflicting_sparse = _candidate(second, leg="sparse", rank=1, score=8.0).model_copy(
        update={"point_id": first.point_id}
    )
    retrieval = _result(dense=(dense,), sparse=(conflicting_sparse,))

    with pytest.raises(FusionIntegrityError, match="conflicting"):
        ReciprocalRankFusionService().fuse(retrieval)


async def test_bypass_constructed_invalid_source_result_is_revalidated() -> None:
    first, _second, _third = await _points()
    candidate = _candidate(first, leg="dense", rank=1, score=0.8)
    valid = _result(mode="dense", dense=(candidate,))
    malformed_rank = valid.model_copy(
        update={"dense_candidates": (candidate.model_copy(update={"rank": 2}),)}
    )
    duplicate = valid.model_copy(
        update={
            "dense_candidates": (
                candidate,
                candidate.model_copy(update={"rank": 2}),
            )
        }
    )
    foreign = valid.model_copy(
        update={
            "dense_candidates": (
                candidate.model_copy(
                    update={
                        "payload": candidate.payload.model_copy(
                            update={"tenant_id": FOREIGN_TENANT_ID}
                        )
                    }
                ),
            )
        }
    )

    for malformed in (malformed_rank, duplicate, foreign):
        with pytest.raises(FusionInputError, match="invalid"):
            ReciprocalRankFusionService().fuse(malformed)
    with pytest.raises(FusionInputError, match="CandidateRetrievalResult"):
        ReciprocalRankFusionService().fuse(object())  # type: ignore[arg-type]
    with pytest.raises(FusionInputError, match="RrfFusionConfig"):
        ReciprocalRankFusionService(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "config_factory",
    [
        lambda: RrfFusionConfig(rrf_k=0),
        lambda: RrfFusionConfig(rrf_k=True),
        lambda: RrfFusionConfig(fused_limit=0),
        lambda: RrfFusionConfig(fused_limit=101),
    ],
)
def test_invalid_fusion_configuration_fails_closed(
    config_factory: Callable[[], RrfFusionConfig],
) -> None:
    with pytest.raises(FusionInputError):
        config_factory()


async def test_fusion_models_reject_nonfinite_and_wrong_formula_values() -> None:
    first, _second, _third = await _points()
    with pytest.raises(ValidationError):
        FusionContribution(
            leg="dense",
            source_rank=1,
            source_score=float("nan"),
            reciprocal_rank_score=1 / 61,
        )

    valid = ReciprocalRankFusionService().fuse(
        _result(
            mode="dense",
            dense=(_candidate(first, leg="dense", rank=1, score=0.8),),
        )
    )
    candidate = valid.candidates[0]
    wrong_contribution = candidate.contributions[0].model_copy(
        update={"reciprocal_rank_score": 1.0}
    )
    tampered_candidate = candidate.model_copy(
        update={"fused_score": 1.0, "contributions": (wrong_contribution,)}
    )
    with pytest.raises(ValidationError, match="configured RRF k"):
        ReciprocalRankFusionResult(
            **valid.model_dump(exclude={"candidates"}),
            candidates=(tampered_candidate,),
        )


async def test_fused_candidate_contract_rejects_duplicate_leg_explanations() -> None:
    first, _second, _third = await _points()
    contribution = FusionContribution(
        leg="dense",
        source_rank=1,
        source_score=0.8,
        reciprocal_rank_score=1 / 61,
    )

    with pytest.raises(ValidationError, match="repeat"):
        FusedCandidate(
            point_id=first.point_id,
            rank=1,
            fused_score=2 / 61,
            best_source_rank=1,
            contributions=(contribution, contribution),
            payload=first.payload,
        )
