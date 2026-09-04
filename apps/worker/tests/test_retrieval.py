"""Proof for tenant-scoped provider-neutral candidate retrieval."""

import hashlib
from collections.abc import Callable
from uuid import UUID

import pytest
from pydantic import ValidationError

from apps.worker.tests.test_vector_index import (
    FOREIGN_TENANT_ID,
    TENANT_ID,
    VERSION_ID,
    _request,
)
from openwikirag.application.embeddings import (
    DenseEmbedding,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
)
from openwikirag.application.retrieval import (
    CandidateRetrievalConfig,
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    RetrievalDependencyError,
    RetrievalInputError,
    RetrievalOutputError,
    RetrievalProviderError,
    SearchCandidate,
    SearchFilters,
    SearchRequest,
)
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbedding,
    SparseEmbeddingConfig,
    SparseEmbeddingRequest,
)
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorIndexDependencyError,
    VectorPoint,
)

OTHER_VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")


def _config() -> CandidateRetrievalConfig:
    return CandidateRetrievalConfig(
        dense=EmbeddingConfig(model_identity="dense-test-v1", dimensions=8),
        sparse=SparseEmbeddingConfig(
            model_identity="sparse-test-v1",
            index_space_size=32,
        ),
        collection=VectorCollectionConfig(
            dense_dimensions=8,
            sparse_index_space_size=32,
        ),
    )


async def _points() -> tuple[VectorPoint, ...]:
    exact = (
        await _request(text="JWT refresh tokens rotate after authentication.")
    ).build_point()
    other = (
        await _request(
            document_version_id=OTHER_VERSION_ID,
            text="Redis leases reclaim abandoned ingestion jobs.",
        )
    ).build_point()
    foreign = (
        await _request(
            tenant_id=FOREIGN_TENANT_ID,
            text="JWT refresh tokens rotate after authentication.",
        )
    ).build_point()
    markdown_payload = other.payload.model_copy(update={"source_type": "markdown"})
    markdown = other.model_copy(update={"payload": markdown_payload})
    return exact, markdown, foreign


def test_query_normalization_and_checksum_are_deterministic() -> None:
    first = SearchRequest(
        tenant_id=TENANT_ID,
        query="  ＪＷＴ\trefresh\n tokens  ",
    )
    second = SearchRequest(
        tenant_id=TENANT_ID,
        query="JWT refresh tokens",
    )

    assert first.normalized_query == "JWT refresh tokens"
    assert first.query_checksum_sha256 == second.query_checksum_sha256
    assert first.query_checksum_sha256 == hashlib.sha256(
        b"JWT refresh tokens"
    ).hexdigest()


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: SearchRequest(tenant_id=TENANT_ID, query=" \n\t "),
        lambda: SearchRequest(tenant_id=TENANT_ID, query="bad\x00query"),
        lambda: SearchRequest(tenant_id=TENANT_ID, query="x" * 4_097),
        lambda: SearchRequest(tenant_id=TENANT_ID, query="valid", candidate_limit=0),
        lambda: SearchRequest(
            tenant_id=TENANT_ID,
            query="valid",
            mode="unknown",  # type: ignore[arg-type]
        ),
        lambda: SearchRequest(
            tenant_id=TENANT_ID,
            query="valid",
            filters=SearchFilters(source_types=("pdf", "pdf")),
        ),
        lambda: SearchRequest(
            tenant_id=TENANT_ID,
            query="valid",
            filters=SearchFilters(
                document_ids=tuple(UUID(int=value) for value in range(51))
            ),
        ),
    ],
)
def test_invalid_queries_limits_modes_and_filters_fail_closed(
    request_factory: Callable[[], SearchRequest],
) -> None:
    with pytest.raises(RetrievalInputError):
        request_factory()


class RecordingDenseProvider:
    provider_identity = "deterministic-hash-provider-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        self.calls += 1
        return await DeterministicHashEmbeddingProvider().embed(request)


class RecordingSparseProvider:
    provider_identity = "deterministic-hash-sparse-provider-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, request: SparseEmbeddingRequest) -> SparseEmbedding:
        self.calls += 1
        return await DeterministicHashSparseEmbeddingProvider().embed(request)


async def test_dense_mode_calls_only_dense_leg() -> None:
    dense = RecordingDenseProvider()
    sparse = RecordingSparseProvider()
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
        dense_provider=dense,
        sparse_provider=sparse,
    )

    result = await service.retrieve(
        SearchRequest(
            tenant_id=TENANT_ID,
            query="JWT refresh tokens rotate after authentication.",
            mode="dense",
        )
    )

    assert dense.calls == 1
    assert sparse.calls == 0
    assert result.sparse_candidates == ()
    assert result.dense_candidates[0].payload.document_version_id == VERSION_ID
    assert result.dense_candidates[0].score == pytest.approx(1.0)


async def test_sparse_mode_calls_only_sparse_leg_and_preserves_exact_terms() -> None:
    dense = RecordingDenseProvider()
    sparse = RecordingSparseProvider()
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
        dense_provider=dense,
        sparse_provider=sparse,
    )

    result = await service.retrieve(
        SearchRequest(
            tenant_id=TENANT_ID,
            query="JWT authentication",
            mode="sparse",
        )
    )

    assert dense.calls == 0
    assert sparse.calls == 1
    assert result.dense_candidates == ()
    assert tuple(candidate.rank for candidate in result.sparse_candidates) == tuple(
        range(1, len(result.sparse_candidates) + 1)
    )
    assert result.sparse_candidates[0].payload.document_version_id == VERSION_ID
    assert result.sparse_candidates[0].score > result.sparse_candidates[-1].score


async def test_hybrid_mode_returns_separate_deterministic_rankings() -> None:
    request = SearchRequest(
        tenant_id=TENANT_ID,
        query="JWT refresh tokens rotate after authentication.",
        mode="hybrid",
    )
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
    )

    first = await service.retrieve(request)
    second = await service.retrieve(request)

    assert first == second
    assert first.dense_candidates
    assert first.sparse_candidates
    assert all(candidate.leg == "dense" for candidate in first.dense_candidates)
    assert all(candidate.leg == "sparse" for candidate in first.sparse_candidates)
    assert tuple(candidate.rank for candidate in first.dense_candidates) == tuple(
        range(1, len(first.dense_candidates) + 1)
    )
    assert tuple(candidate.rank for candidate in first.sparse_candidates) == tuple(
        range(1, len(first.sparse_candidates) + 1)
    )


async def test_dense_ties_use_stable_point_id_order() -> None:
    text = "Identical representation creates an exact score tie."
    first = (await _request(text=text)).build_point()
    second = (
        await _request(document_version_id=OTHER_VERSION_ID, text=text)
    ).build_point()
    service = CandidateRetrievalService(
        InMemoryCandidateIndex((second, first)),
        config=_config(),
    )

    result = await service.retrieve(
        SearchRequest(tenant_id=TENANT_ID, query=text, mode="dense")
    )

    returned_ids = tuple(candidate.point_id for candidate in result.dense_candidates)
    assert returned_ids == tuple(sorted((first.point_id, second.point_id)))
    assert result.dense_candidates[0].score == pytest.approx(
        result.dense_candidates[1].score
    )


async def test_tenant_and_optional_filters_exclude_nonmatching_points() -> None:
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
    )
    result = await service.retrieve(
        SearchRequest(
            tenant_id=TENANT_ID,
            query="Redis leases",
            filters=SearchFilters(
                document_version_ids=(OTHER_VERSION_ID,),
                source_types=("markdown",),
                languages=("en",),
                chunk_kinds=("parent",),
                pipeline_versions=("ingestion-v1",),
            ),
            mode="hybrid",
        )
    )

    candidates = result.dense_candidates + result.sparse_candidates
    assert candidates
    assert all(candidate.payload.tenant_id == TENANT_ID for candidate in candidates)
    assert all(
        candidate.payload.document_version_id == OTHER_VERSION_ID
        for candidate in candidates
    )
    assert all(candidate.payload.source_type == "markdown" for candidate in candidates)

    foreign = await service.retrieve(
        SearchRequest(
            tenant_id=FOREIGN_TENANT_ID,
            query="Redis leases",
            filters=SearchFilters(source_types=("markdown",)),
        )
    )
    assert foreign.dense_candidates == ()
    assert foreign.sparse_candidates == ()


class FailingDenseProvider:
    provider_identity = "failing-dense-v1"

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        del request
        raise RuntimeError("provider unavailable")


async def test_provider_failure_is_typed() -> None:
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
        dense_provider=FailingDenseProvider(),
    )

    with pytest.raises(RetrievalProviderError):
        await service.retrieve(
            SearchRequest(tenant_id=TENANT_ID, query="identity", mode="dense")
        )


class IncompatibleDenseProvider:
    provider_identity = "deterministic-hash-provider-v1"

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        result = await DeterministicHashEmbeddingProvider().embed(request)
        return result.model_copy(update={"input_checksum_sha256": "f" * 64})


async def test_incompatible_provider_output_is_rejected() -> None:
    service = CandidateRetrievalService(
        InMemoryCandidateIndex(await _points()),
        config=_config(),
        dense_provider=IncompatibleDenseProvider(),
    )

    with pytest.raises(RetrievalOutputError):
        await service.retrieve(
            SearchRequest(tenant_id=TENANT_ID, query="identity", mode="dense")
        )


class FailingCandidateIndex:
    async def search_dense(
        self,
        *,
        tenant_id: UUID,
        query: DenseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        del tenant_id, query, filters, limit
        raise VectorIndexDependencyError("candidate store unavailable")

    async def search_sparse(
        self,
        *,
        tenant_id: UUID,
        query: SparseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        del tenant_id, query, filters, limit
        raise VectorIndexDependencyError("candidate store unavailable")


async def test_index_dependency_failure_is_typed() -> None:
    service = CandidateRetrievalService(FailingCandidateIndex(), config=_config())

    with pytest.raises(RetrievalDependencyError):
        await service.retrieve(
            SearchRequest(tenant_id=TENANT_ID, query="identity", mode="dense")
        )


class MalformedCandidateIndex:
    def __init__(self, candidate: SearchCandidate) -> None:
        self.candidate = candidate

    async def search_dense(
        self,
        *,
        tenant_id: UUID,
        query: DenseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        del tenant_id, query, filters, limit
        return (self.candidate.model_copy(update={"rank": 2}),)

    async def search_sparse(
        self,
        *,
        tenant_id: UUID,
        query: SparseEmbedding,
        filters: SearchFilters,
        limit: int,
    ) -> tuple[SearchCandidate, ...]:
        del tenant_id, query, filters, limit
        return ()


async def test_malformed_index_ranking_is_rejected() -> None:
    point = (await _request()).build_point()
    candidate = SearchCandidate(
        point_id=point.point_id,
        leg="dense",
        rank=1,
        score=1.0,
        payload=point.payload,
    )
    service = CandidateRetrievalService(
        MalformedCandidateIndex(candidate),
        config=_config(),
    )

    with pytest.raises(RetrievalOutputError):
        await service.retrieve(
            SearchRequest(tenant_id=TENANT_ID, query="identity", mode="dense")
        )


async def test_candidate_rejects_nonfinite_scores() -> None:
    point = (await _request()).build_point()
    with pytest.raises(ValidationError):
        SearchCandidate(
            point_id="11111111-1111-5111-8111-111111111111",
            leg="dense",
            rank=1,
            score=float("nan"),
            payload=point.payload,
        )


def test_configuration_rejects_query_collection_geometry_mismatch() -> None:
    with pytest.raises(RetrievalInputError, match="dimensions"):
        CandidateRetrievalConfig(
            dense=EmbeddingConfig(dimensions=8),
            collection=VectorCollectionConfig(dense_dimensions=16),
        )
