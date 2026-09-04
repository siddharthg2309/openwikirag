"""Provider-neutral tenant-scoped candidate retrieval contracts."""

import hashlib
import math
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.chunking import ChunkKind
from openwikirag.application.embeddings import (
    DenseEmbedding,
    DenseEmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingError,
    EmbeddingRequest,
    validate_embedding_result,
)
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbedding,
    SparseEmbeddingConfig,
    SparseEmbeddingError,
    SparseEmbeddingProvider,
    SparseEmbeddingRequest,
    validate_sparse_embedding_result,
)
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorIndexDependencyError,
    VectorIndexError,
    VectorPoint,
    VectorPointPayload,
)

RetrievalMode = Literal["dense", "sparse", "hybrid"]
RetrievalLeg = Literal["dense", "sparse"]
MAX_QUERY_CHARACTERS = 4_096
MAX_FILTER_VALUES = 50
MAX_RETRIEVAL_CANDIDATES = 100


class RetrievalError(Exception):
    """Base error for candidate retrieval."""


class RetrievalInputError(RetrievalError):
    """Raised before provider/index work when a request is malformed."""


class RetrievalProviderError(RetrievalError):
    """Raised when a query representation provider cannot complete."""


class RetrievalDependencyError(RetrievalError):
    """Raised when the candidate index dependency cannot complete."""


class RetrievalOutputError(RetrievalError):
    """Raised when a provider or index returns incompatible candidate data."""


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Bounded, immutable filters applied after mandatory tenant scope."""

    document_ids: tuple[UUID, ...] = ()
    document_version_ids: tuple[UUID, ...] = ()
    source_types: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    chunk_kinds: tuple[ChunkKind, ...] = ()
    pipeline_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_filter_values("document_ids", self.document_ids, UUID)
        _validate_filter_values("document_version_ids", self.document_version_ids, UUID)
        _validate_filter_values("source_types", self.source_types, str, max_length=64)
        _validate_filter_values("languages", self.languages, str, max_length=16)
        _validate_filter_values(
            "pipeline_versions",
            self.pipeline_versions,
            str,
            max_length=255,
        )
        _validate_filter_values("chunk_kinds", self.chunk_kinds, str, max_length=16)
        if any(kind not in {"parent", "child"} for kind in self.chunk_kinds):
            raise RetrievalInputError("Chunk-kind filters must be parent or child.")

    def matches(self, payload: VectorPointPayload) -> bool:
        """Return whether a validated payload satisfies every optional filter."""

        return (
            (not self.document_ids or payload.document_id in self.document_ids)
            and (
                not self.document_version_ids
                or payload.document_version_id in self.document_version_ids
            )
            and (not self.source_types or payload.source_type in self.source_types)
            and (not self.languages or payload.language in self.languages)
            and (not self.chunk_kinds or payload.chunk_kind in self.chunk_kinds)
            and (
                not self.pipeline_versions
                or payload.pipeline_version in self.pipeline_versions
            )
        )


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Trusted tenant scope plus normalized, bounded candidate-search input."""

    tenant_id: UUID
    query: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    mode: RetrievalMode = "hybrid"
    candidate_limit: int = 20
    normalized_query: str = field(init=False)
    query_checksum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise RetrievalInputError("Search requests require a UUID tenant id.")
        if not isinstance(self.query, str):
            raise RetrievalInputError("Search queries must be text.")
        if not isinstance(self.filters, SearchFilters):
            raise RetrievalInputError("Search requests require validated filters.")
        if self.mode not in {"dense", "sparse", "hybrid"}:
            raise RetrievalInputError("Search mode must be dense, sparse, or hybrid.")
        if (
            type(self.candidate_limit) is not int
            or not 1 <= self.candidate_limit <= MAX_RETRIEVAL_CANDIDATES
        ):
            raise RetrievalInputError(
                f"Candidate limit must be between 1 and {MAX_RETRIEVAL_CANDIDATES}."
            )

        unicode_query = unicodedata.normalize("NFKC", self.query)
        if any(
            unicodedata.category(character) in {"Cc", "Cf"}
            and not character.isspace()
            for character in unicode_query
        ):
            raise RetrievalInputError("Search queries cannot contain control characters.")
        normalized = " ".join(unicode_query.split())
        if not normalized:
            raise RetrievalInputError("Search queries cannot be empty.")
        if len(normalized) > MAX_QUERY_CHARACTERS:
            raise RetrievalInputError(
                f"Search queries cannot exceed {MAX_QUERY_CHARACTERS} characters."
            )
        object.__setattr__(self, "normalized_query", normalized)
        object.__setattr__(
            self,
            "query_checksum_sha256",
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )


class SearchCandidate(BaseModel):
    """One scored projection candidate with complete source provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    leg: RetrievalLeg
    rank: int = Field(ge=1, le=MAX_RETRIEVAL_CANDIDATES)
    score: float
    payload: VectorPointPayload

    @model_validator(mode="after")
    def validate_score(self) -> Self:
        if not math.isfinite(self.score):
            raise ValueError("Retrieval candidate scores must be finite.")
        return self


class CandidateRetrievalResult(BaseModel):
    """Separate candidate lists ready for a later fusion stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    normalized_query: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)
    query_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: RetrievalMode
    candidate_limit: int = Field(ge=1, le=MAX_RETRIEVAL_CANDIDATES)
    dense_candidates: tuple[SearchCandidate, ...] = ()
    sparse_candidates: tuple[SearchCandidate, ...] = ()

    @model_validator(mode="after")
    def validate_lists(self) -> Self:
        _validate_result_list(
            self.dense_candidates,
            leg="dense",
            tenant_id=self.tenant_id,
            limit=self.candidate_limit,
        )
        _validate_result_list(
            self.sparse_candidates,
            leg="sparse",
            tenant_id=self.tenant_id,
            limit=self.candidate_limit,
        )
        if self.mode == "dense" and self.sparse_candidates:
            raise ValueError("Dense mode cannot contain sparse candidates.")
        if self.mode == "sparse" and self.dense_candidates:
            raise ValueError("Sparse mode cannot contain dense candidates.")
        return self


class CandidateIndex(Protocol):
    """Application-owned port for independently ranked vector-search legs."""

    async def search_dense(
        self,
        *,
        tenant_id: UUID,
        query: DenseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        """Return one tenant-filtered dense candidate list."""

    async def search_sparse(
        self,
        *,
        tenant_id: UUID,
        query: SparseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        """Return one tenant-filtered sparse candidate list."""


def _default_sparse_config() -> SparseEmbeddingConfig:
    return SparseEmbeddingConfig(index_space_size=2**20)


@dataclass(frozen=True, slots=True)
class CandidateRetrievalConfig:
    """Server-owned query representation and collection compatibility."""

    dense: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    sparse: SparseEmbeddingConfig = field(default_factory=_default_sparse_config)
    collection: VectorCollectionConfig = field(default_factory=VectorCollectionConfig)

    def __post_init__(self) -> None:
        if self.dense.dimensions != self.collection.dense_dimensions:
            raise RetrievalInputError(
                "Dense query dimensions must match the candidate collection."
            )
        if self.dense.distance_metric != self.collection.dense_distance_metric:
            raise RetrievalInputError(
                "Dense query distance must match the candidate collection."
            )
        if self.sparse.index_space_size != self.collection.sparse_index_space_size:
            raise RetrievalInputError(
                "Sparse query space must match the candidate collection."
            )
        if self.sparse.distance_metric != self.collection.sparse_distance_metric:
            raise RetrievalInputError(
                "Sparse query distance must match the candidate collection."
            )


class CandidateRetrievalService:
    """Generate only requested query representations and validate each leg."""

    def __init__(
        self,
        index: CandidateIndex,
        *,
        config: CandidateRetrievalConfig | None = None,
        dense_provider: DenseEmbeddingProvider | None = None,
        sparse_provider: SparseEmbeddingProvider | None = None,
    ) -> None:
        self._index = index
        self._config = config or CandidateRetrievalConfig()
        self._dense_provider = dense_provider or DeterministicHashEmbeddingProvider()
        self._sparse_provider = sparse_provider or DeterministicHashSparseEmbeddingProvider()

    async def retrieve(self, request: SearchRequest) -> CandidateRetrievalResult:
        """Return validated dense/sparse lists without fusing their scores."""

        if not isinstance(request, SearchRequest):
            raise RetrievalInputError("Candidate retrieval requires a SearchRequest.")

        dense_candidates: tuple[SearchCandidate, ...] = ()
        sparse_candidates: tuple[SearchCandidate, ...] = ()
        if request.mode in {"dense", "hybrid"}:
            dense_candidates = await self._retrieve_dense(request)
        if request.mode in {"sparse", "hybrid"}:
            sparse_candidates = await self._retrieve_sparse(request)
        return CandidateRetrievalResult(
            tenant_id=request.tenant_id,
            normalized_query=request.normalized_query,
            query_checksum_sha256=request.query_checksum_sha256,
            mode=request.mode,
            candidate_limit=request.candidate_limit,
            dense_candidates=dense_candidates,
            sparse_candidates=sparse_candidates,
        )

    async def _retrieve_dense(
        self,
        request: SearchRequest,
    ) -> tuple[SearchCandidate, ...]:
        embedding_request = EmbeddingRequest(
            text=request.normalized_query,
            input_checksum_sha256=request.query_checksum_sha256,
            config=self._config.dense,
        )
        try:
            query = validate_embedding_result(
                request=embedding_request,
                result=await self._dense_provider.embed(embedding_request),
            )
        except EmbeddingError as exc:
            raise RetrievalOutputError("The dense query output is incompatible.") from exc
        except Exception as exc:
            raise RetrievalProviderError("The dense query provider failed.") from exc
        candidates = await self._search_dense(request=request, query=query)
        return _validate_index_output(
            candidates,
            request=request,
            leg="dense",
            config_checksum=query.config_checksum_sha256,
            model_identity=query.model_identity,
            provider_identity=query.provider_identity,
        )

    async def _retrieve_sparse(
        self,
        request: SearchRequest,
    ) -> tuple[SearchCandidate, ...]:
        embedding_request = SparseEmbeddingRequest(
            text=request.normalized_query,
            input_checksum_sha256=request.query_checksum_sha256,
            config=self._config.sparse,
        )
        try:
            query = validate_sparse_embedding_result(
                request=embedding_request,
                result=await self._sparse_provider.embed(embedding_request),
            )
        except SparseEmbeddingError as exc:
            raise RetrievalOutputError("The sparse query output is incompatible.") from exc
        except Exception as exc:
            raise RetrievalProviderError("The sparse query provider failed.") from exc
        candidates = await self._search_sparse(request=request, query=query)
        return _validate_index_output(
            candidates,
            request=request,
            leg="sparse",
            config_checksum=query.config_checksum_sha256,
            model_identity=query.model_identity,
            provider_identity=query.provider_identity,
        )

    async def _search_dense(
        self,
        *,
        request: SearchRequest,
        query: DenseEmbedding,
    ) -> tuple[SearchCandidate, ...]:
        try:
            return await self._index.search_dense(
                tenant_id=request.tenant_id,
                query=query,
                filters=request.filters,
                limit=request.candidate_limit,
            )
        except VectorIndexDependencyError as exc:
            raise RetrievalDependencyError("The dense candidate index failed.") from exc
        except VectorIndexError as exc:
            raise RetrievalOutputError("The dense candidate index rejected the query.") from exc
        except Exception as exc:
            raise RetrievalDependencyError("The dense candidate index failed.") from exc

    async def _search_sparse(
        self,
        *,
        request: SearchRequest,
        query: SparseEmbedding,
    ) -> tuple[SearchCandidate, ...]:
        try:
            return await self._index.search_sparse(
                tenant_id=request.tenant_id,
                query=query,
                filters=request.filters,
                limit=request.candidate_limit,
            )
        except VectorIndexDependencyError as exc:
            raise RetrievalDependencyError("The sparse candidate index failed.") from exc
        except VectorIndexError as exc:
            raise RetrievalOutputError("The sparse candidate index rejected the query.") from exc
        except Exception as exc:
            raise RetrievalDependencyError("The sparse candidate index failed.") from exc


class InMemoryCandidateIndex:
    """Deterministic scoring adapter used for contract and ranking proof."""

    def __init__(self, points: Iterable[VectorPoint]) -> None:
        try:
            validated = tuple(points)
        except TypeError as exc:
            raise RetrievalInputError("Candidate points must be iterable.") from exc
        if any(not isinstance(point, VectorPoint) for point in validated):
            raise RetrievalInputError("Candidate indexes require validated vector points.")
        self._points = validated

    async def search_dense(
        self,
        *,
        tenant_id: UUID,
        query: DenseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        """Rank matching unit vectors by deterministic dot product."""

        scored = (
            (
                point,
                sum(
                    left * right
                    for left, right in zip(
                        query.vector,
                        point.dense.vector,
                        strict=True,
                    )
                ),
            )
            for point in self._eligible_points(
                tenant_id=tenant_id,
                filters=filters,
                leg="dense",
                config_checksum=query.config_checksum_sha256,
                model_identity=query.model_identity,
                provider_identity=query.provider_identity,
            )
        )
        return _rank_candidates(scored, leg="dense", limit=limit)

    async def search_sparse(
        self,
        *,
        tenant_id: UUID,
        query: SparseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        """Rank candidates by matching sparse-index dot product."""

        query_values = dict(zip(query.indices, query.values, strict=True))
        scored: list[tuple[VectorPoint, float]] = []
        for point in self._eligible_points(
            tenant_id=tenant_id,
            filters=filters,
            leg="sparse",
            config_checksum=query.config_checksum_sha256,
            model_identity=query.model_identity,
            provider_identity=query.provider_identity,
        ):
            score = sum(
                query_values.get(index, 0.0) * value
                for index, value in zip(
                    point.sparse.indices,
                    point.sparse.values,
                    strict=True,
                )
            )
            if score > 0.0:
                scored.append((point, score))
        return _rank_candidates(scored, leg="sparse", limit=limit)

    def _eligible_points(
        self,
        *,
        tenant_id: UUID,
        filters: SearchFilters,
        leg: RetrievalLeg,
        config_checksum: str,
        model_identity: str,
        provider_identity: str,
    ) -> tuple[VectorPoint, ...]:
        result: list[VectorPoint] = []
        for point in self._points:
            payload = point.payload
            if payload.tenant_id != tenant_id or not filters.matches(payload):
                continue
            if leg == "dense":
                identity_matches = (
                    payload.dense_config_checksum_sha256 == config_checksum
                    and payload.dense_model_identity == model_identity
                    and payload.dense_provider_identity == provider_identity
                )
            else:
                identity_matches = (
                    payload.sparse_config_checksum_sha256 == config_checksum
                    and payload.sparse_model_identity == model_identity
                    and payload.sparse_provider_identity == provider_identity
                )
            if identity_matches:
                result.append(point)
        return tuple(result)


def _rank_candidates(
    scored: Iterable[tuple[VectorPoint, float]],
    *,
    leg: RetrievalLeg,
    limit: int,
) -> tuple[SearchCandidate, ...]:
    ordered = sorted(scored, key=lambda item: (-item[1], item[0].point_id))[:limit]
    return tuple(
        SearchCandidate(
            point_id=point.point_id,
            leg=leg,
            rank=rank,
            score=score,
            payload=point.payload,
        )
        for rank, (point, score) in enumerate(ordered, start=1)
    )


def _validate_filter_values(
    name: str,
    values: tuple[object, ...],
    expected_type: type[object],
    *,
    max_length: int | None = None,
) -> None:
    if not isinstance(values, tuple):
        raise RetrievalInputError(f"{name} filters must be an immutable tuple.")
    if len(values) > MAX_FILTER_VALUES:
        raise RetrievalInputError(
            f"{name} cannot contain more than {MAX_FILTER_VALUES} values."
        )
    if len(values) != len(set(values)):
        raise RetrievalInputError(f"{name} filters cannot contain duplicates.")
    if any(not isinstance(value, expected_type) for value in values):
        raise RetrievalInputError(f"{name} contains an invalid value type.")
    if expected_type is str:
        strings = tuple(value for value in values if isinstance(value, str))
        if any(not value.strip() for value in strings):
            raise RetrievalInputError(f"{name} cannot contain empty values.")
        if max_length is not None and any(len(value) > max_length for value in strings):
            raise RetrievalInputError(f"{name} contains an oversized value.")


def _validate_result_list(
    candidates: tuple[SearchCandidate, ...],
    *,
    leg: RetrievalLeg,
    tenant_id: UUID,
    limit: int,
) -> None:
    if len(candidates) > limit:
        raise ValueError("A retrieval leg exceeded its candidate limit.")
    if tuple(candidate.rank for candidate in candidates) != tuple(
        range(1, len(candidates) + 1)
    ):
        raise ValueError("Retrieval candidate ranks must be contiguous.")
    if any(candidate.leg != leg for candidate in candidates):
        raise ValueError("A retrieval candidate belongs to the wrong leg.")
    if any(candidate.payload.tenant_id != tenant_id for candidate in candidates):
        raise ValueError("A retrieval candidate crossed the tenant boundary.")
    point_ids = tuple(candidate.point_id for candidate in candidates)
    if len(point_ids) != len(set(point_ids)):
        raise ValueError("A retrieval leg cannot contain duplicate points.")


def _validate_index_output(
    candidates: object,
    *,
    request: SearchRequest,
    leg: RetrievalLeg,
    config_checksum: str,
    model_identity: str,
    provider_identity: str,
) -> tuple[SearchCandidate, ...]:
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, SearchCandidate) for candidate in candidates
    ):
        raise RetrievalOutputError("Candidate indexes must return validated tuples.")
    try:
        _validate_result_list(
            candidates,
            leg=leg,
            tenant_id=request.tenant_id,
            limit=request.candidate_limit,
        )
    except ValueError as exc:
        raise RetrievalOutputError("The candidate index returned an invalid ranking.") from exc
    for candidate in candidates:
        payload = candidate.payload
        if not request.filters.matches(payload):
            raise RetrievalOutputError("The candidate index violated a requested filter.")
        if leg == "dense":
            matches_identity = (
                payload.dense_config_checksum_sha256 == config_checksum
                and payload.dense_model_identity == model_identity
                and payload.dense_provider_identity == provider_identity
            )
        else:
            matches_identity = (
                payload.sparse_config_checksum_sha256 == config_checksum
                and payload.sparse_model_identity == model_identity
                and payload.sparse_provider_identity == provider_identity
            )
        if not matches_identity:
            raise RetrievalOutputError(
                "The candidate index mixed an incompatible representation identity."
            )
    return candidates


__all__ = [
    "CandidateIndex",
    "CandidateRetrievalConfig",
    "CandidateRetrievalResult",
    "CandidateRetrievalService",
    "InMemoryCandidateIndex",
    "MAX_FILTER_VALUES",
    "MAX_QUERY_CHARACTERS",
    "MAX_RETRIEVAL_CANDIDATES",
    "RetrievalDependencyError",
    "RetrievalError",
    "RetrievalInputError",
    "RetrievalLeg",
    "RetrievalMode",
    "RetrievalOutputError",
    "RetrievalProviderError",
    "SearchCandidate",
    "SearchFilters",
    "SearchRequest",
]
