"""Ollama adapter for semantic dense embeddings."""

import json
import math
import re
from typing import Any

import httpx
from pydantic import ValidationError

from openwikirag.application.embeddings import (
    DenseEmbedding,
    EmbeddingInputError,
    EmbeddingOutputError,
    EmbeddingProviderError,
    EmbeddingRequest,
    validate_embedding_result,
)

OLLAMA_EMBEDDING_PROVIDER_VERSION = "ollama-embedding-api-v1"
MAX_EMBEDDING_RESPONSE_BYTES = 512 * 1024


class OllamaDenseEmbeddingProvider:
    """Generate one unit dense vector through a digest-checked Ollama model."""

    def __init__(
        self,
        *,
        model: str,
        digest: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(
                "Ollama embeddings require a model name and expected immutable digest."
            )
        parsed_url = httpx.URL(base_url)
        if parsed_url.scheme not in {"http", "https"} or parsed_url.username or parsed_url.password:
            raise ValueError("Ollama embeddings require an HTTP(S) URL without credentials.")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("Ollama embedding timeout must be between 0 and 300 seconds.")
        self.model = model.strip()
        self.digest = digest
        self.base_url = str(parsed_url).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def model_identity(self) -> str:
        """Return the model identity used in the canonical vector payload."""

        return f"{self.model}@{self.digest}"

    @property
    def provider_identity(self) -> str:
        """Return the immutable adapter/model identity captured in vector points."""

        return (
            f"ollama-embedding:{self.model}:expected-digest={self.digest}:"
            f"{OLLAMA_EMBEDDING_PROVIDER_VERSION}:truncate-false"
        )

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        """Run bounded identity checks and return one validated unit vector."""

        if not isinstance(request, EmbeddingRequest):
            raise EmbeddingInputError("Embedding providers require an EmbeddingRequest.")
        if request.config.model_identity != self.model_identity:
            raise EmbeddingInputError("Embedding configuration does not match the provider model.")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                await self._assert_model(client)
                result = await self._read(
                    client,
                    "POST",
                    "/api/embed",
                    limit=MAX_EMBEDDING_RESPONSE_BYTES,
                    payload={
                        "model": self.model,
                        "input": request.text,
                        "dimensions": request.config.dimensions,
                        "truncate": False,
                    },
                )
                vector = _parse_vector(
                    result,
                    model=self.model,
                    dimensions=request.config.dimensions,
                )
                embedding = DenseEmbedding(
                    provider_identity=self.provider_identity,
                    model_identity=self.model_identity,
                    config_checksum_sha256=request.config.checksum_sha256,
                    input_checksum_sha256=request.input_checksum_sha256,
                    dimensions=request.config.dimensions,
                    distance_metric=request.config.distance_metric,
                    vector=vector,
                )
                await self._assert_model(client)
                return validate_embedding_result(request=request, result=embedding)
        except (EmbeddingInputError, EmbeddingOutputError, EmbeddingProviderError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise EmbeddingProviderError("The Ollama embedding dependency is unavailable.") from exc
        except ValidationError as exc:
            raise EmbeddingOutputError("The Ollama embedding vector is invalid.") from exc
        except Exception as exc:
            raise EmbeddingProviderError("The Ollama embedding provider failed.") from exc

    async def _assert_model(self, client: httpx.AsyncClient) -> None:
        tags = await self._read(client, "GET", "/api/tags", limit=65_536)
        models = tags.get("models")
        if not isinstance(models, list):
            raise EmbeddingOutputError("Ollama tags response is malformed.")
        if not any(
            item.get("name") == self.model and item.get("digest") == self.digest
            for item in models
            if isinstance(item, dict)
        ):
            raise EmbeddingOutputError(
                "Configured Ollama embedding model is unavailable or changed."
            )

    @staticmethod
    async def _read(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        limit: int,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        async with client.stream(method, path, json=payload) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise EmbeddingProviderError(
                    "The Ollama embedding service is temporarily unavailable."
                )
            if response.status_code != 200:
                raise EmbeddingOutputError("The Ollama embedding request was rejected.")
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > limit:
                    raise EmbeddingOutputError(
                        "The Ollama embedding response exceeds the byte limit."
                    )
            try:
                value = json.loads(data)
            except json.JSONDecodeError as exc:
                raise EmbeddingOutputError(
                    "The Ollama embedding response is not valid JSON."
                ) from exc
            if not isinstance(value, dict):
                raise EmbeddingOutputError(
                    "The Ollama embedding response must be a JSON object."
                )
            return value


def _parse_vector(
    result: dict[str, Any],
    *,
    model: str,
    dimensions: int,
) -> tuple[float, ...]:
    if result.get("model") != model:
        raise EmbeddingOutputError("Ollama embedding response model does not match the request.")
    embeddings = result.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != 1:
        raise EmbeddingOutputError("Ollama must return exactly one embedding vector.")
    vector = embeddings[0]
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise EmbeddingOutputError("Ollama embedding dimensions do not match the request.")
    if any(type(value) not in {int, float} or not math.isfinite(float(value)) for value in vector):
        raise EmbeddingOutputError("Ollama embedding values must be finite numbers.")
    return tuple(float(value) for value in vector)


__all__ = [
    "MAX_EMBEDDING_RESPONSE_BYTES",
    "OLLAMA_EMBEDDING_PROVIDER_VERSION",
    "OllamaDenseEmbeddingProvider",
]
