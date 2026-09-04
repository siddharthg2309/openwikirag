"""Evidence deduplication preserves version boundaries and ranking explanations."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.worker.tests.test_fusion import _candidate, _points, _result
from apps.worker.tests.test_vector_index import FOREIGN_TENANT_ID
from openwikirag.application.deduplication import (
    DeduplicatedEvidence,
    EvidenceDeduplicationError,
    deduplicate_evidence,
)
from openwikirag.application.fusion import ReciprocalRankFusionResult, ReciprocalRankFusionService


async def _fusion() -> ReciprocalRankFusionResult:
    first, second, _ = await _points()
    duplicate = first.model_copy(update={"point_id": str(uuid4())})
    return ReciprocalRankFusionService().fuse(
        _result(
            mode="dense",
            dense=(
                _candidate(first, leg="dense", rank=1, score=3),
                _candidate(duplicate, leg="dense", rank=2, score=2),
                _candidate(second, leg="dense", rank=3, score=1),
            ),
        )
    )


async def test_duplicate_projections_preserve_winner_and_full_explanation() -> None:
    fused = await _fusion()
    result = deduplicate_evidence(fused)
    assert len(result.groups) == 2
    assert result.duplicate_count == 1
    assert result.groups[0].representative == fused.candidates[0]
    assert result.groups[0].duplicates == (fused.candidates[1],)
    assert result.groups[1].representative.rank == 3
    assert tuple(group.rank for group in result.groups) == (1, 2)
    assert deduplicate_evidence(fused) == result


async def test_different_versions_and_distinct_chunks_are_not_collapsed() -> None:
    fused = await _fusion()
    second = fused.candidates[1]
    for changes in ({"document_version_id": uuid4()}, {"chunk_id": "chunk-" + "a" * 64}):
        modified = second.model_copy(update={"payload": second.payload.model_copy(update=changes)})
        result = deduplicate_evidence(
            fused.model_copy(
                update={
                    "candidates": (fused.candidates[0], modified, fused.candidates[2]),
                }
            )
        )
        assert len(result.groups) == 3


async def test_representation_changes_do_not_change_evidence_identity() -> None:
    fused = await _fusion()
    second = fused.candidates[1]
    modified = second.model_copy(
        update={
            "payload": second.payload.model_copy(
                update={
                    "dense_model_identity": "new-model",
                    "dense_dimensions": 256,
                }
            )
        }
    )
    result = deduplicate_evidence(
        fused.model_copy(
            update={
                "candidates": (fused.candidates[0], modified, fused.candidates[2]),
            }
        )
    )
    assert result.duplicate_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_checksum_sha256", "b" * 64),
        ("document_id", uuid4()),
        ("source_artifact_checksum", "c" * 64),
        ("title", "Conflicting title"),
        ("tenant_id", FOREIGN_TENANT_ID),
        ("pipeline_version", "other-pipeline"),
    ],
)
async def test_conflicting_or_foreign_evidence_fails_closed(field: str, value: object) -> None:
    fused = await _fusion()
    second = fused.candidates[1]
    modified = second.model_copy(
        update={"payload": second.payload.model_copy(update={field: value})}
    )
    with pytest.raises(EvidenceDeduplicationError):
        deduplicate_evidence(
            fused.model_copy(
                update={
                    "candidates": (fused.candidates[0], modified, fused.candidates[2]),
                }
            )
        )


async def test_invalid_source_and_tampered_partition_are_rejected() -> None:
    fused = await _fusion()
    with pytest.raises(EvidenceDeduplicationError):
        deduplicate_evidence(object())  # type: ignore[arg-type]
    with pytest.raises(EvidenceDeduplicationError):
        deduplicate_evidence(
            fused.model_copy(update={"candidates": tuple(reversed(fused.candidates))})
        )
    result = deduplicate_evidence(fused)
    data = result.model_dump(mode="python")
    data["groups"] = data["groups"][:1]
    with pytest.raises(ValidationError, match="partition"):
        DeduplicatedEvidence.model_validate(data)


def test_empty_fusion_is_valid_without_side_effects() -> None:
    result = deduplicate_evidence(ReciprocalRankFusionService().fuse(_result()))
    assert result.groups == ()
    assert result.duplicate_count == 0
