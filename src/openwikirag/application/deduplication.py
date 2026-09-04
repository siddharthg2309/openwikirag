"""Version-safe evidence identity grouping after reciprocal-rank fusion."""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openwikirag.application.fusion import (
    MAX_FUSED_CANDIDATES,
    FusedCandidate,
    ReciprocalRankFusionResult,
)
from openwikirag.application.vector_index import VectorPointPayload

# Only representation fields may differ for two projections of the same evidence.
_PROJECTION_FIELDS = {
    "collection_config_checksum_sha256",
    "dense_dimensions",
    "sparse_index_space_size",
    "sparse_token_count",
    "dense_provider_identity",
    "dense_model_identity",
    "dense_config_checksum_sha256",
    "sparse_provider_identity",
    "sparse_model_identity",
    "sparse_config_checksum_sha256",
}


class EvidenceDeduplicationError(Exception):
    """Malformed input or conflicting immutable evidence; never silently discard it."""


class EvidenceGroup(BaseModel):
    """Highest-ranked projection and the lower-ranked projections it replaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1, le=MAX_FUSED_CANDIDATES)
    representative: FusedCandidate
    duplicates: tuple[FusedCandidate, ...] = Field(default=(), max_length=99)

    @model_validator(mode="after")
    def validate_group(self) -> Self:
        winner = self.representative
        seen = {winner.point_id}
        previous_rank = winner.rank
        for duplicate in self.duplicates:
            if duplicate.point_id in seen or duplicate.rank <= previous_rank:
                raise ValueError("Duplicate projections must follow the winner in fused order.")
            if evidence_identity(duplicate.payload) != evidence_identity(winner.payload):
                raise ValueError("A duplicate must share canonical evidence identity.")
            if source_provenance(duplicate.payload) != source_provenance(winner.payload):
                raise ValueError("Duplicate evidence has conflicting source provenance.")
            seen.add(duplicate.point_id)
            previous_rank = duplicate.rank
        return self


class DeduplicatedEvidence(BaseModel):
    """Full bounded fusion lineage plus a lossless, auditable evidence grouping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fusion: ReciprocalRankFusionResult
    groups: tuple[EvidenceGroup, ...] = Field(default=(), max_length=MAX_FUSED_CANDIDATES)

    @property
    def duplicate_count(self) -> int:
        return len(self.fusion.candidates) - len(self.groups)

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if tuple(group.rank for group in self.groups) != tuple(range(1, len(self.groups) + 1)):
            raise ValueError("Evidence group ranks must be contiguous.")
        identities = [evidence_identity(group.representative.payload) for group in self.groups]
        if len(set(identities)) != len(identities):
            raise ValueError("Evidence groups must have unique canonical identities.")
        winner_ranks = tuple(group.representative.rank for group in self.groups)
        if winner_ranks != tuple(sorted(winner_ranks)):
            raise ValueError("Evidence groups must preserve fused order.")
        members = [
            candidate
            for group in self.groups
            for candidate in (group.representative, *group.duplicates)
        ]
        if tuple(sorted(members, key=lambda item: item.rank)) != self.fusion.candidates:
            raise ValueError("Groups must partition the exact fused candidate set.")
        return self


def evidence_identity(payload: VectorPointPayload) -> tuple[UUID, UUID, str]:
    """A chunk id alone does not identify the tenant or immutable document version."""

    return payload.tenant_id, payload.document_version_id, payload.chunk_id


def source_provenance(payload: VectorPointPayload) -> dict[str, object]:
    """Compare every canonical field, excluding only explicit vector-specific fields."""

    return payload.model_dump(mode="python", exclude=_PROJECTION_FIELDS)


def deduplicate_evidence(fusion: ReciprocalRankFusionResult) -> DeduplicatedEvidence:
    """Keep the strongest projection without adding its duplicates' RRF scores."""

    if not isinstance(fusion, ReciprocalRankFusionResult):
        raise EvidenceDeduplicationError("Expected a ReciprocalRankFusionResult.")
    try:
        validated = ReciprocalRankFusionResult.model_validate(fusion.model_dump(mode="python"))
        groups: dict[tuple[UUID, UUID, str], list[FusedCandidate]] = {}
        for candidate in validated.candidates:
            groups.setdefault(evidence_identity(candidate.payload), []).append(candidate)
        return DeduplicatedEvidence(
            fusion=validated,
            groups=tuple(
                EvidenceGroup(rank=rank, representative=items[0], duplicates=tuple(items[1:]))
                for rank, items in enumerate(groups.values(), start=1)
            ),
        )
    except ValidationError as exc:
        raise EvidenceDeduplicationError(
            "Invalid fusion or conflicting evidence provenance."
        ) from exc
