"""Proof for the provider-neutral sparse lexical contract."""

import hashlib

import pytest
from pydantic import ValidationError

from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbedding,
    SparseEmbeddingConfig,
    SparseEmbeddingConfigurationError,
    SparseEmbeddingInputError,
    SparseEmbeddingOutputError,
    SparseEmbeddingRequest,
    validate_sparse_embedding_result,
)


def _request(
    text: str = "JWT refresh tokens rotate after authentication.",
    *,
    config: SparseEmbeddingConfig | None = None,
) -> SparseEmbeddingRequest:
    return SparseEmbeddingRequest(
        text=text,
        input_checksum_sha256=hashlib.sha256(text.encode()).hexdigest(),
        config=config or SparseEmbeddingConfig(index_space_size=2**20),
    )


async def test_sparse_provider_is_reproducible_with_sorted_unique_indices() -> None:
    request = _request()
    provider = DeterministicHashSparseEmbeddingProvider()

    first = await provider.embed(request)
    second = await provider.embed(request)

    assert first.indices == second.indices
    assert first.values == second.values
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert first.token_count == 6
    assert first.index_count == len(first.indices) == len(first.values)
    assert first.indices == tuple(sorted(set(first.indices)))
    assert all(value > 0.0 for value in first.values)


async def test_casefolding_merges_terms_and_repeated_terms_increase_weight() -> None:
    provider = DeterministicHashSparseEmbeddingProvider()
    folded = await provider.embed(_request("JWT jwt rotation"))
    once = await provider.embed(_request("JWT"))
    repeated = await provider.embed(_request("JWT JWT"))

    assert folded.token_count == 3
    assert folded.index_count == 2
    assert repeated.indices == once.indices
    assert repeated.values[0] > once.values[0]


async def test_result_records_lexical_reproducibility_identity() -> None:
    request = _request()
    result = await DeterministicHashSparseEmbeddingProvider().embed(request)

    assert result.schema_version == "sparse-embedding-v1"
    assert result.provider_identity == "deterministic-hash-sparse-provider-v1"
    assert result.model_identity == request.config.model_identity
    assert result.input_checksum_sha256 == request.input_checksum_sha256
    assert result.config_checksum_sha256 == request.config.checksum_sha256
    assert result.index_space_size == request.config.index_space_size
    assert result.distance_metric == "dot"


async def test_changed_sparse_configuration_creates_a_distinct_identity() -> None:
    text = "The retrieval index preserves exact tenant identifiers."
    first_request = _request(text, config=SparseEmbeddingConfig(index_space_size=2**16))
    second_request = _request(text, config=SparseEmbeddingConfig(index_space_size=2**20))
    provider = DeterministicHashSparseEmbeddingProvider()

    first = await provider.embed(first_request)
    second = await provider.embed(second_request)

    assert first.config_checksum_sha256 != second.config_checksum_sha256
    assert first.checksum_sha256 != second.checksum_sha256
    assert first.index_space_size == 2**16
    assert second.index_space_size == 2**20


def test_sparse_request_validates_exact_input_and_configuration() -> None:
    with pytest.raises(SparseEmbeddingInputError):
        SparseEmbeddingRequest(
            text="   ",
            input_checksum_sha256="a" * 64,
            config=SparseEmbeddingConfig(),
        )
    with pytest.raises(SparseEmbeddingInputError):
        SparseEmbeddingRequest(
            text="not the hashed input",
            input_checksum_sha256="a" * 64,
            config=SparseEmbeddingConfig(),
        )
    with pytest.raises(SparseEmbeddingConfigurationError):
        SparseEmbeddingConfig(index_space_size=0)
    with pytest.raises(SparseEmbeddingConfigurationError):
        SparseEmbeddingConfig(index_space_size=2**32 + 1)
    with pytest.raises(SparseEmbeddingConfigurationError):
        SparseEmbeddingConfig(distance_metric="cosine")  # type: ignore[arg-type]


def test_sparse_embedding_rejects_malformed_geometry() -> None:
    def build(
        *,
        indices: tuple[int, ...],
        values: tuple[float, ...],
        token_count: int = 2,
        index_count: int = 2,
    ) -> SparseEmbedding:
        return SparseEmbedding(
            provider_identity="test-provider",
            model_identity="test-model",
            config_checksum_sha256="a" * 64,
            input_checksum_sha256="b" * 64,
            index_space_size=16,
            distance_metric="dot",
            token_count=token_count,
            index_count=index_count,
            indices=indices,
            values=values,
        )

    with pytest.raises(ValidationError):
        build(indices=(1,), values=(1.0, 2.0), index_count=1)
    with pytest.raises(ValidationError):
        build(indices=(2, 1), values=(1.0, 1.0))
    with pytest.raises(ValidationError):
        build(indices=(1, 1), values=(1.0, 1.0))
    with pytest.raises(ValidationError):
        build(indices=(16, 17), values=(1.0, 1.0))
    with pytest.raises(ValidationError):
        build(indices=(1, 2), values=(float("nan"), 1.0))
    with pytest.raises(ValidationError):
        build(indices=(1, 2), values=(0.0, 1.0))


async def test_provider_rejects_wrong_request_type_and_whitespace_input() -> None:
    provider = DeterministicHashSparseEmbeddingProvider()
    with pytest.raises(SparseEmbeddingInputError):
        await provider.embed(object())  # type: ignore[arg-type]
    with pytest.raises(SparseEmbeddingInputError):
        SparseEmbeddingRequest(
            text="\n\t",
            input_checksum_sha256=hashlib.sha256(b"\n\t").hexdigest(),
            config=SparseEmbeddingConfig(),
        )


async def test_provider_output_identity_is_checked_against_request() -> None:
    request = _request()
    result = await DeterministicHashSparseEmbeddingProvider().embed(request)
    mismatched = result.model_copy(update={"input_checksum_sha256": "c" * 64})

    with pytest.raises(SparseEmbeddingOutputError):
        validate_sparse_embedding_result(request=request, result=mismatched)


def test_sparse_validation_rejects_non_sparse_provider_output() -> None:
    request = _request()
    with pytest.raises(SparseEmbeddingOutputError):
        validate_sparse_embedding_result(request=request, result=object())  # type: ignore[arg-type]
