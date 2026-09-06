"""Proof for the optional semantic Ollama dense-embedding adapter."""

import hashlib
import json

import httpx
import pytest

from openwikirag.application.embeddings import (
    EmbeddingConfig,
    EmbeddingInputError,
    EmbeddingOutputError,
    EmbeddingProviderError,
    EmbeddingRequest,
)
from openwikirag.core.config import Settings
from openwikirag.infrastructure.embedding_factory import (
    build_dense_embedding_config,
    build_dense_embedding_provider,
)
from openwikirag.infrastructure.ollama_embeddings import OllamaDenseEmbeddingProvider

MODEL = "embeddinggemma"
DIGEST = "a" * 64
MODEL_IDENTITY = f"{MODEL}@{DIGEST}"


def _request(*, dimensions: int = 3, model_identity: str = MODEL_IDENTITY) -> EmbeddingRequest:
    text = "semantic retrieval evidence"
    return EmbeddingRequest(
        text=text,
        input_checksum_sha256=hashlib.sha256(text.encode()).hexdigest(),
        config=EmbeddingConfig(model_identity=model_identity, dimensions=dimensions),
    )


def _transport(
    *,
    drift_after_embed: bool = False,
    embed_status: int = 200,
    embeddings: object = ((0.6, 0.8, 0.0),),
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    tag_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tag_calls
        calls.append(request)
        if request.url.path == "/api/tags":
            tag_calls += 1
            digest = "b" * 64 if drift_after_embed and tag_calls == 2 else DIGEST
            return httpx.Response(
                200,
                json={"models": [{"name": MODEL, "digest": digest}]},
                request=request,
            )
        if request.url.path == "/api/embed":
            return httpx.Response(
                embed_status,
                content=json.dumps(
                    {"model": MODEL, "embeddings": embeddings},
                    allow_nan=True,
                ).encode(),
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler), calls


def _provider(transport: httpx.AsyncBaseTransport) -> OllamaDenseEmbeddingProvider:
    return OllamaDenseEmbeddingProvider(
        model=MODEL,
        digest=DIGEST,
        base_url="http://ollama.test",
        transport=transport,
    )


@pytest.mark.anyio
async def test_provider_checks_identity_and_returns_unit_vector() -> None:
    transport, calls = _transport()
    result = await _provider(transport).embed(_request())

    assert result.vector == pytest.approx((0.6, 0.8, 0.0))
    assert result.model_identity == MODEL_IDENTITY
    assert result.provider_identity.startswith("ollama-embedding:embeddinggemma:")
    assert [call.url.path for call in calls] == ["/api/tags", "/api/embed", "/api/tags"]
    payload = json.loads(calls[1].content)
    assert payload == {
        "dimensions": 3,
        "input": "semantic retrieval evidence",
        "model": MODEL,
        "truncate": False,
    }


@pytest.mark.anyio
async def test_provider_normalizes_finite_non_unit_vector() -> None:
    transport, _ = _transport(embeddings=((3.0, 4.0, 0.0),))

    result = await _provider(transport).embed(_request())

    assert result.vector == pytest.approx((0.6, 0.8, 0.0))


@pytest.mark.anyio
async def test_provider_rejects_digest_drift_after_embedding() -> None:
    transport, calls = _transport(drift_after_embed=True)

    with pytest.raises(EmbeddingOutputError, match="unavailable or changed"):
        await _provider(transport).embed(_request())

    assert [call.url.path for call in calls] == ["/api/tags", "/api/embed", "/api/tags"]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.anyio
async def test_provider_maps_transient_http_failures(status: int) -> None:
    transport, _ = _transport(embed_status=status)

    with pytest.raises(EmbeddingProviderError, match="temporarily unavailable"):
        await _provider(transport).embed(_request())


@pytest.mark.parametrize(
    "embeddings",
    [
        ((0.6, 0.8),),
        ((0.0, 0.0, 0.0),),
        ((float("nan"), 0.0, 0.0),),
        (("0.6", 0.8, 0.0),),
        ((0.6, 0.8, 0.0), (0.0, 1.0, 0.0)),
    ],
)
@pytest.mark.anyio
async def test_provider_rejects_invalid_embedding_geometry(embeddings: object) -> None:
    transport, _ = _transport(embeddings=embeddings)

    with pytest.raises(EmbeddingOutputError):
        await _provider(transport).embed(_request())


@pytest.mark.anyio
async def test_provider_maps_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(EmbeddingProviderError, match="unavailable"):
        await _provider(httpx.MockTransport(handler)).embed(_request())


def test_factory_keeps_hash_default_and_composes_one_semantic_identity() -> None:
    default_settings = Settings()
    default = build_dense_embedding_provider(default_settings)
    assert default.__class__.__name__ == "DeterministicHashEmbeddingProvider"
    assert build_dense_embedding_config(default_settings).model_identity == "hashing-model-v1"

    semantic_settings = Settings(
        dense_embedding_model=MODEL,
        dense_embedding_model_digest=DIGEST,
        dense_embedding_dimensions=3,
    )
    config = build_dense_embedding_config(semantic_settings)
    provider = build_dense_embedding_provider(semantic_settings)
    assert config.model_identity == MODEL_IDENTITY
    assert config.dimensions == 3
    assert provider.provider_identity.startswith("ollama-embedding:embeddinggemma:")


def test_factory_rejects_incomplete_identity() -> None:
    with pytest.raises(ValueError, match="together"):
        Settings(dense_embedding_model=MODEL)


@pytest.mark.asyncio
async def test_provider_rejects_mismatched_configuration_identity() -> None:
    provider = _provider(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    with pytest.raises(EmbeddingInputError, match="does not match"):
        await provider.embed(_request(model_identity="wrong@" + DIGEST))
