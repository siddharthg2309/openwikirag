"""Proof for the provider-neutral dense-embedding contract."""

import hashlib
import math

import pytest
from pydantic import ValidationError

from openwikirag.application.embeddings import (
    DenseEmbedding,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingConfigurationError,
    EmbeddingInputError,
    EmbeddingOutputError,
    EmbeddingRequest,
    validate_embedding_result,
)


def _request(
    text: str = "JWT refresh tokens rotate after authentication.",
    *,
    config: EmbeddingConfig | None = None,
) -> EmbeddingRequest:
    return EmbeddingRequest(
        text=text,
        input_checksum_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        config=config or EmbeddingConfig(dimensions=32),
    )


async def test_hash_provider_is_reproducible_and_cosine_normalized() -> None:
    request = _request()
    provider = DeterministicHashEmbeddingProvider()

    first = await provider.embed(request)
    second = await provider.embed(request)

    assert first.vector == second.vector
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert len(first.vector) == request.config.dimensions
    assert math.isclose(
        math.sqrt(sum(value * value for value in first.vector)),
        1.0,
        rel_tol=1e-6,
        abs_tol=1e-6,
    )


async def test_result_records_the_full_reproducibility_identity() -> None:
    request = _request()
    result = await DeterministicHashEmbeddingProvider().embed(request)

    assert result.schema_version == "dense-embedding-v1"
    assert result.provider_identity == "deterministic-hash-provider-v1"
    assert result.model_identity == request.config.model_identity
    assert result.input_checksum_sha256 == request.input_checksum_sha256
    assert result.config_checksum_sha256 == request.config.checksum_sha256
    assert result.dimensions == 32
    assert result.distance_metric == "cosine"


async def test_changed_embedding_configuration_creates_a_distinct_identity() -> None:
    text = "The retrieval index uses tenant-aware evidence filters."
    first_request = _request(text, config=EmbeddingConfig(dimensions=16))
    second_request = _request(text, config=EmbeddingConfig(dimensions=24))
    provider = DeterministicHashEmbeddingProvider()

    first = await provider.embed(first_request)
    second = await provider.embed(second_request)

    assert first.config_checksum_sha256 != second.config_checksum_sha256
    assert first.checksum_sha256 != second.checksum_sha256
    assert first.dimensions == 16
    assert second.dimensions == 24


def test_embedding_request_validates_exact_input_and_configuration() -> None:
    with pytest.raises(EmbeddingInputError):
        EmbeddingRequest(
            text="   ",
            input_checksum_sha256="a" * 64,
            config=EmbeddingConfig(),
        )
    with pytest.raises(EmbeddingInputError):
        EmbeddingRequest(
            text="not the hashed input",
            input_checksum_sha256="a" * 64,
            config=EmbeddingConfig(),
        )
    with pytest.raises(EmbeddingConfigurationError):
        EmbeddingConfig(dimensions=0)
    with pytest.raises(EmbeddingConfigurationError):
        EmbeddingConfig(dimensions=8_193)
    with pytest.raises(EmbeddingConfigurationError):
        EmbeddingConfig(distance_metric="dot")  # type: ignore[arg-type]


def test_dense_embedding_rejects_malformed_vector_geometry() -> None:
    def build(vector: tuple[float, ...]) -> DenseEmbedding:
        return DenseEmbedding(
            provider_identity="test-provider",
            model_identity="test-model",
            config_checksum_sha256="a" * 64,
            input_checksum_sha256="b" * 64,
            dimensions=2,
            distance_metric="cosine",
            vector=vector,
        )

    with pytest.raises(ValidationError):
        build((1.0,))
    with pytest.raises(ValidationError):
        build((float("nan"), 0.0))
    with pytest.raises(ValidationError):
        build((0.0, 0.0))
    with pytest.raises(ValidationError):
        build((2.0, 0.0))


async def test_provider_rejects_wrong_request_type_and_empty_tokens() -> None:
    provider = DeterministicHashEmbeddingProvider()
    with pytest.raises(EmbeddingInputError):
        await provider.embed(object())  # type: ignore[arg-type]

    text = "\n\t"
    with pytest.raises(EmbeddingInputError):
        EmbeddingRequest(
            text=text,
            input_checksum_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            config=EmbeddingConfig(dimensions=8),
        )


async def test_provider_output_identity_is_checked_against_request() -> None:
    request = _request()
    result = await DeterministicHashEmbeddingProvider().embed(request)
    mismatched = result.model_copy(
        update={"input_checksum_sha256": "c" * 64},
    )

    with pytest.raises(EmbeddingOutputError):
        validate_embedding_result(request=request, result=mismatched)


def test_embedding_validation_rejects_non_dense_provider_output() -> None:
    request = _request()
    with pytest.raises(EmbeddingOutputError):
        validate_embedding_result(request=request, result=object())  # type: ignore[arg-type]
