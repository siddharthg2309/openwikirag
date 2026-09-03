"""Provider-neutral sparse lexical contracts and an offline deterministic adapter."""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SPARSE_EMBEDDING_SCHEMA_VERSION: Literal["sparse-embedding-v1"] = "sparse-embedding-v1"
SPARSE_CONFIG_SCHEMA_VERSION: Literal["sparse-config-v1"] = "sparse-config-v1"
SparseDistanceMetric = Literal["dot"]
MAX_SPARSE_INDEX_SPACE = 2**32
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"\b\w+\b", re.UNICODE)


class SparseEmbeddingError(Exception):
    """Base error for sparse lexical contract and provider failures."""


class SparseEmbeddingInputError(SparseEmbeddingError):
    """Raised when a sparse embedding request is malformed or empty."""


class SparseEmbeddingConfigurationError(SparseEmbeddingError):
    """Raised when sparse index geometry or metric settings are unsupported."""


class SparseEmbeddingOutputError(SparseEmbeddingError):
    """Raised when provider output does not match its request contract."""


@dataclass(frozen=True, slots=True)
class SparseEmbeddingConfig:
    """Server-owned identity and index geometry for one sparse projection."""

    model_identity: str = "lexical-hash-v1"
    index_space_size: int = MAX_SPARSE_INDEX_SPACE
    distance_metric: SparseDistanceMetric = "dot"

    def __post_init__(self) -> None:
        if not isinstance(self.model_identity, str) or not self.model_identity.strip():
            raise SparseEmbeddingConfigurationError("Sparse model identity cannot be empty.")
        if self.index_space_size < 1 or self.index_space_size > MAX_SPARSE_INDEX_SPACE:
            raise SparseEmbeddingConfigurationError(
                f"Sparse index space must be between 1 and {MAX_SPARSE_INDEX_SPACE}."
            )
        if self.distance_metric != "dot":
            raise SparseEmbeddingConfigurationError(
                "Only dot product is supported by this sparse contract."
            )

    def canonical_payload(self) -> dict[str, int | str]:
        """Return the stable configuration representation used in identities."""

        return {
            "schema_version": SPARSE_CONFIG_SCHEMA_VERSION,
            "model_identity": self.model_identity,
            "index_space_size": self.index_space_size,
            "distance_metric": self.distance_metric,
        }

    def canonical_bytes(self) -> bytes:
        """Serialize the validated sparse configuration deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the stable checksum of this sparse configuration."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class SparseEmbeddingRequest:
    """Exact text and configuration submitted to a sparse provider."""

    text: str
    input_checksum_sha256: str
    config: SparseEmbeddingConfig

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise SparseEmbeddingInputError("Sparse embedding input text cannot be empty.")
        if not _CHECKSUM_PATTERN.fullmatch(self.input_checksum_sha256):
            raise SparseEmbeddingInputError("The sparse input checksum is invalid.")
        if _text_checksum(self.text) != self.input_checksum_sha256:
            raise SparseEmbeddingInputError(
                "The sparse input checksum does not match its text."
            )
        if not isinstance(self.config, SparseEmbeddingConfig):
            raise SparseEmbeddingInputError(
                "Sparse embedding requests require a SparseEmbeddingConfig."
            )


class SparseEmbedding(BaseModel):
    """Immutable sparse index/value pairs with the identity needed for indexing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sparse-embedding-v1"] = SPARSE_EMBEDDING_SCHEMA_VERSION
    provider_identity: str = Field(min_length=1, max_length=255)
    model_identity: str = Field(min_length=1, max_length=255)
    config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_space_size: int = Field(gt=0, le=MAX_SPARSE_INDEX_SPACE)
    distance_metric: SparseDistanceMetric
    token_count: int = Field(gt=0)
    index_count: int = Field(gt=0)
    indices: tuple[int, ...]
    values: tuple[float, ...]

    @model_validator(mode="after")
    def validate_sparse_vector(self) -> Self:
        """Enforce sorted sparse geometry and finite positive weights."""

        if self.index_count != len(self.indices) or len(self.indices) != len(self.values):
            raise ValueError("Sparse indices and values must have matching counts.")
        if self.token_count < self.index_count:
            raise ValueError("Sparse token count cannot be smaller than index count.")
        if any(index < 0 or index >= self.index_space_size for index in self.indices):
            raise ValueError("Sparse indices must be inside the configured index space.")
        if tuple(sorted(set(self.indices))) != self.indices:
            raise ValueError("Sparse indices must be sorted and unique.")
        if any(not math.isfinite(value) or value <= 0.0 for value in self.values):
            raise ValueError("Sparse values must be finite and positive.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible sparse representation."""

        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        """Serialize sparse values deterministically for later persistence."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the identity of the exact validated sparse bytes."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class SparseEmbeddingProvider(Protocol):
    """Application-owned port implemented by local or remote sparse providers."""

    provider_identity: str

    async def embed(self, request: SparseEmbeddingRequest) -> SparseEmbedding:
        """Create one validated sparse representation for exact request text."""


@dataclass(frozen=True, slots=True)
class DeterministicHashSparseEmbeddingProvider:
    """Offline lexical baseline using hashed terms and sublinear term frequency."""

    provider_identity: str = "deterministic-hash-sparse-provider-v1"

    async def embed(self, request: SparseEmbeddingRequest) -> SparseEmbedding:
        """Case-fold, hash, and weight lexical terms into a sparse vector."""

        if not isinstance(request, SparseEmbeddingRequest):
            raise SparseEmbeddingInputError(
                "Sparse providers require a SparseEmbeddingRequest."
            )

        tokens = tuple(_TOKEN_PATTERN.findall(request.text.casefold()))
        if not tokens:
            raise SparseEmbeddingInputError("Sparse input requires at least one lexical term.")

        term_counts = Counter(tokens)
        index_counts: Counter[int] = Counter()
        for term, count in term_counts.items():
            index_counts[_term_index(term, request.config.index_space_size)] += count

        ordered = tuple(sorted(index_counts.items()))
        result = SparseEmbedding(
            provider_identity=self.provider_identity,
            model_identity=request.config.model_identity,
            config_checksum_sha256=request.config.checksum_sha256,
            input_checksum_sha256=request.input_checksum_sha256,
            index_space_size=request.config.index_space_size,
            distance_metric=request.config.distance_metric,
            token_count=len(tokens),
            index_count=len(ordered),
            indices=tuple(index for index, _ in ordered),
            values=tuple(1.0 + math.log(count) for _, count in ordered),
        )
        return validate_sparse_embedding_result(request=request, result=result)


def validate_sparse_embedding_result(
    *,
    request: SparseEmbeddingRequest,
    result: SparseEmbedding,
) -> SparseEmbedding:
    """Validate provider identity fields against the exact sparse request."""

    if not isinstance(request, SparseEmbeddingRequest):
        raise SparseEmbeddingInputError(
            "Sparse embedding validation requires a SparseEmbeddingRequest."
        )
    if not isinstance(result, SparseEmbedding):
        raise SparseEmbeddingOutputError(
            "Sparse providers must return SparseEmbedding."
        )
    if result.input_checksum_sha256 != request.input_checksum_sha256:
        raise SparseEmbeddingOutputError("Sparse output does not match the input checksum.")
    if result.config_checksum_sha256 != request.config.checksum_sha256:
        raise SparseEmbeddingOutputError("Sparse output does not match the configuration.")
    if result.model_identity != request.config.model_identity:
        raise SparseEmbeddingOutputError("Sparse output does not match the model identity.")
    if result.index_space_size != request.config.index_space_size:
        raise SparseEmbeddingOutputError("Sparse output does not match the index space.")
    if result.distance_metric != request.config.distance_metric:
        raise SparseEmbeddingOutputError("Sparse output does not match the configured metric.")
    return result


def _term_index(term: str, index_space_size: int) -> int:
    digest = hashlib.sha256(term.encode()).digest()
    return int.from_bytes(digest[:4], byteorder="big") % index_space_size


def _text_checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "DeterministicHashSparseEmbeddingProvider",
    "SPARSE_CONFIG_SCHEMA_VERSION",
    "SPARSE_EMBEDDING_SCHEMA_VERSION",
    "SparseEmbedding",
    "SparseEmbeddingConfig",
    "SparseEmbeddingConfigurationError",
    "SparseEmbeddingError",
    "SparseEmbeddingInputError",
    "SparseEmbeddingOutputError",
    "SparseEmbeddingProvider",
    "SparseEmbeddingRequest",
    "MAX_SPARSE_INDEX_SPACE",
    "validate_sparse_embedding_result",
]
