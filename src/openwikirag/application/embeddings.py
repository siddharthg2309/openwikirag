"""Provider-neutral dense embedding contracts and an offline deterministic adapter."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EMBEDDING_SCHEMA_VERSION: Literal["dense-embedding-v1"] = "dense-embedding-v1"
EMBEDDING_CONFIG_SCHEMA_VERSION: Literal["embedding-config-v1"] = "embedding-config-v1"
DistanceMetric = Literal["cosine"]
MAX_EMBEDDING_DIMENSIONS = 8_192
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"\S+")


class EmbeddingError(Exception):
    """Base error for embedding contract and provider failures."""


class EmbeddingInputError(EmbeddingError):
    """Raised when an embedding request is malformed or empty."""


class EmbeddingConfigurationError(EmbeddingError):
    """Raised when embedding dimensions or distance settings are unsupported."""


class EmbeddingOutputError(EmbeddingError):
    """Raised when a provider result does not match its request contract."""


class EmbeddingProviderError(EmbeddingError):
    """Raised when an embedding dependency cannot complete a request."""


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Server-owned identity and geometry for one dense embedding projection."""

    model_identity: str = "hashing-model-v1"
    dimensions: int = 128
    distance_metric: DistanceMetric = "cosine"

    def __post_init__(self) -> None:
        if not isinstance(self.model_identity, str) or not self.model_identity.strip():
            raise EmbeddingConfigurationError("Embedding model identity cannot be empty.")
        if self.dimensions < 1 or self.dimensions > MAX_EMBEDDING_DIMENSIONS:
            raise EmbeddingConfigurationError(
                f"Embedding dimensions must be between 1 and {MAX_EMBEDDING_DIMENSIONS}."
            )
        if self.distance_metric != "cosine":
            raise EmbeddingConfigurationError(
                "Only cosine distance is supported by this embedding contract."
            )

    def canonical_payload(self) -> dict[str, int | str]:
        """Return the stable configuration representation used in identities."""

        return {
            "schema_version": EMBEDDING_CONFIG_SCHEMA_VERSION,
            "model_identity": self.model_identity,
            "dimensions": self.dimensions,
            "distance_metric": self.distance_metric,
        }

    def canonical_bytes(self) -> bytes:
        """Serialize the validated configuration deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the stable checksum of this configuration."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """Exact text and configuration submitted to an embedding provider."""

    text: str
    input_checksum_sha256: str
    config: EmbeddingConfig

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise EmbeddingInputError("Embedding input text cannot be empty.")
        if not _CHECKSUM_PATTERN.fullmatch(self.input_checksum_sha256):
            raise EmbeddingInputError("The embedding input checksum is invalid.")
        if _text_checksum(self.text) != self.input_checksum_sha256:
            raise EmbeddingInputError("The embedding input checksum does not match its text.")
        if not isinstance(self.config, EmbeddingConfig):
            raise EmbeddingInputError("Embedding requests require an EmbeddingConfig.")


class DenseEmbedding(BaseModel):
    """Immutable dense vector with the identity needed for safe indexing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["dense-embedding-v1"] = EMBEDDING_SCHEMA_VERSION
    provider_identity: str = Field(min_length=1, max_length=255)
    model_identity: str = Field(min_length=1, max_length=255)
    config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimensions: int = Field(gt=0, le=MAX_EMBEDDING_DIMENSIONS)
    distance_metric: DistanceMetric
    vector: tuple[float, ...]

    @model_validator(mode="after")
    def validate_vector(self) -> Self:
        """Enforce the geometry promised by the dense-vector contract."""

        if len(self.vector) != self.dimensions:
            raise ValueError("Dense embedding vector length does not match dimensions.")
        if any(not math.isfinite(value) for value in self.vector):
            raise ValueError("Dense embedding values must be finite.")
        norm = math.sqrt(sum(value * value for value in self.vector))
        if norm == 0.0:
            raise ValueError("Dense embedding vector cannot have zero norm.")
        if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("Cosine dense embeddings must have unit L2 norm.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible vector representation."""

        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        """Serialize the vector deterministically for later persistence."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the identity of the exact validated embedding bytes."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class DenseEmbeddingProvider(Protocol):
    """Application-owned port implemented by local or remote dense providers."""

    @property
    def provider_identity(self) -> str:
        """Return the stable provider/model identity."""

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        """Create one validated dense embedding for the exact request text."""


@dataclass(frozen=True, slots=True)
class DeterministicHashEmbeddingProvider:
    """Offline feature-hashing baseline that proves shape, identity, and replayability."""

    provider_identity: str = "deterministic-hash-provider-v1"

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        """Hash normalized tokens into a cosine-normalized deterministic vector."""

        if not isinstance(request, EmbeddingRequest):
            raise EmbeddingInputError("Embedding providers require an EmbeddingRequest.")

        tokens = tuple(_TOKEN_PATTERN.findall(request.text.casefold()))
        if not tokens:
            raise EmbeddingInputError("Embedding input requires at least one token.")

        vector = [0.0] * request.config.dimensions
        for ordinal, token in enumerate(tokens):
            digest = hashlib.sha256(f"{ordinal}\0{token}".encode()).digest()
            index = int.from_bytes(digest[:8], byteorder="big") % request.config.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0
            norm = 1.0
        result = DenseEmbedding(
            provider_identity=self.provider_identity,
            model_identity=request.config.model_identity,
            config_checksum_sha256=request.config.checksum_sha256,
            input_checksum_sha256=request.input_checksum_sha256,
            dimensions=request.config.dimensions,
            distance_metric=request.config.distance_metric,
            vector=tuple(value / norm for value in vector),
        )
        return validate_embedding_result(request=request, result=result)


def validate_embedding_result(
    *,
    request: EmbeddingRequest,
    result: DenseEmbedding,
) -> DenseEmbedding:
    """Validate provider identity fields against the exact request contract."""

    if not isinstance(request, EmbeddingRequest):
        raise EmbeddingInputError("Embedding validation requires an EmbeddingRequest.")
    if not isinstance(result, DenseEmbedding):
        raise EmbeddingOutputError("Embedding providers must return DenseEmbedding.")
    if result.input_checksum_sha256 != request.input_checksum_sha256:
        raise EmbeddingOutputError("Embedding output does not match the input checksum.")
    if result.config_checksum_sha256 != request.config.checksum_sha256:
        raise EmbeddingOutputError("Embedding output does not match the configuration.")
    if result.model_identity != request.config.model_identity:
        raise EmbeddingOutputError("Embedding output does not match the model identity.")
    if result.dimensions != request.config.dimensions:
        raise EmbeddingOutputError("Embedding output does not match the configured dimensions.")
    if result.distance_metric != request.config.distance_metric:
        raise EmbeddingOutputError("Embedding output does not match the configured metric.")
    return result


def _text_checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DenseEmbedding",
    "DenseEmbeddingProvider",
    "DeterministicHashEmbeddingProvider",
    "EmbeddingConfig",
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingInputError",
    "EmbeddingOutputError",
    "EmbeddingProviderError",
    "EmbeddingRequest",
    "EMBEDDING_CONFIG_SCHEMA_VERSION",
    "EMBEDDING_SCHEMA_VERSION",
    "MAX_EMBEDDING_DIMENSIONS",
    "validate_embedding_result",
]
