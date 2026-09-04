"""Provider-neutral, tenant-scoped projection contracts for hybrid vector indexes."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.chunking import Chunk, ChunkKind
from openwikirag.application.embeddings import (
    MAX_EMBEDDING_DIMENSIONS,
    DenseEmbedding,
)
from openwikirag.application.metadata import DocumentMetadata
from openwikirag.application.sparse import MAX_SPARSE_INDEX_SPACE, SparseEmbedding

VECTOR_COLLECTION_SCHEMA_VERSION: Literal["vector-collection-v1"] = "vector-collection-v1"
VECTOR_POINT_SCHEMA_VERSION: Literal["vector-point-v1"] = "vector-point-v1"
VectorUpsertStatus = Literal["created", "reused"]

_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_POINT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


class VectorIndexError(Exception):
    """Base error for vector projection and index-port failures."""


class VectorIndexInputError(VectorIndexError):
    """Raised when a projection or index request is malformed."""


class VectorIndexConfigurationError(VectorIndexError):
    """Raised when collection geometry or identity is unsupported."""


class VectorIndexDependencyError(VectorIndexError):
    """Raised when the configured vector-store dependency is unavailable."""


class VectorPointConflictError(VectorIndexError):
    """Raised when an immutable point id is reused with different bytes."""

    def __init__(self, *, point_id: str) -> None:
        self.point_id = point_id
        super().__init__(f"Vector point {point_id} already exists with different bytes.")


@dataclass(frozen=True, slots=True)
class VectorCollectionConfig:
    """Server-owned named-vector geometry and identity for one collection."""

    collection_name: str = "openwikirag-vectors-v1"
    dense_dimensions: int = 128
    sparse_index_space_size: int = 2**20
    dense_distance_metric: Literal["cosine"] = "cosine"
    sparse_distance_metric: Literal["dot"] = "dot"

    def __post_init__(self) -> None:
        if not isinstance(self.collection_name, str) or not _COLLECTION_NAME_PATTERN.fullmatch(
            self.collection_name
        ):
            raise VectorIndexConfigurationError(
                "Collection name must be 1-63 characters of letters, numbers, '.', '_', or '-'."
            )
        if (
            type(self.dense_dimensions) is not int
            or not 1 <= self.dense_dimensions <= MAX_EMBEDDING_DIMENSIONS
        ):
            raise VectorIndexConfigurationError(
                f"Dense dimensions must be between 1 and {MAX_EMBEDDING_DIMENSIONS}."
            )
        if (
            type(self.sparse_index_space_size) is not int
            or not 1 <= self.sparse_index_space_size <= MAX_SPARSE_INDEX_SPACE
        ):
            raise VectorIndexConfigurationError(
                f"Sparse index space must be between 1 and {MAX_SPARSE_INDEX_SPACE}."
            )
        if self.dense_distance_metric != "cosine":
            raise VectorIndexConfigurationError("Only cosine dense distance is supported.")
        if self.sparse_distance_metric != "dot":
            raise VectorIndexConfigurationError("Only dot sparse distance is supported.")

    def canonical_payload(self) -> dict[str, int | str]:
        """Return the stable collection schema identity."""

        return {
            "schema_version": VECTOR_COLLECTION_SCHEMA_VERSION,
            "collection_name": self.collection_name,
            "dense_dimensions": self.dense_dimensions,
            "sparse_index_space_size": self.sparse_index_space_size,
            "dense_distance_metric": self.dense_distance_metric,
            "sparse_distance_metric": self.sparse_distance_metric,
        }

    def canonical_bytes(self) -> bytes:
        """Serialize collection geometry deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the identity of this collection schema."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class VectorPointPayload(BaseModel):
    """Metadata Qdrant can filter on without duplicating canonical chunk text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["vector-point-v1"] = VECTOR_POINT_SCHEMA_VERSION
    tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_id: str = Field(pattern=r"^chunk-[0-9a-f]{64}$")
    chunk_kind: ChunkKind
    parent_chunk_id: str | None = Field(default=None, pattern=r"^chunk-[0-9a-f]{64}$")
    section_path: tuple[str, ...] = ()
    title: str = Field(min_length=1, max_length=1_000)
    source_type: str = Field(min_length=1, max_length=64)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    language: str = Field(min_length=1, max_length=16)
    pipeline_version: str = Field(min_length=1, max_length=255)
    source_artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dense_dimensions: int = Field(gt=0)
    sparse_index_space_size: int = Field(gt=0)
    sparse_token_count: int = Field(gt=0)
    dense_provider_identity: str = Field(min_length=1, max_length=255)
    dense_model_identity: str = Field(min_length=1, max_length=255)
    dense_config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sparse_provider_identity: str = Field(min_length=1, max_length=255)
    sparse_model_identity: str = Field(min_length=1, max_length=255)
    sparse_config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        """Keep hierarchy and page metadata internally consistent."""

        if self.chunk_kind == "parent" and self.parent_chunk_id is not None:
            raise ValueError("Parent vector payloads cannot reference a parent chunk.")
        if self.chunk_kind == "child" and self.parent_chunk_id is None:
            raise ValueError("Child vector payloads require a parent chunk id.")
        if self.page_start is not None and self.page_end is not None:
            if self.page_end < self.page_start:
                raise ValueError("Vector payload page range is invalid.")
        if any(not section for section in self.section_path):
            raise ValueError("Vector payload section paths cannot contain empty labels.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return the JSON-compatible Qdrant payload representation."""

        return self.model_dump(mode="json", exclude_none=True)


class VectorPoint(BaseModel):
    """One immutable hybrid point with named dense and sparse vectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["vector-point-v1"] = VECTOR_POINT_SCHEMA_VERSION
    point_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    collection_config_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dense: DenseEmbedding
    sparse: SparseEmbedding
    payload: VectorPointPayload

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Ensure vector and payload identities describe the same projection."""

        if self.collection_config_checksum_sha256 != self.payload.collection_config_checksum_sha256:
            raise ValueError("Vector point collection identity does not match its payload.")
        if self.dense.input_checksum_sha256 != self.payload.content_checksum_sha256:
            raise ValueError("Dense vector input does not match the chunk content.")
        if self.sparse.input_checksum_sha256 != self.payload.content_checksum_sha256:
            raise ValueError("Sparse vector input does not match the chunk content.")
        if self.dense.provider_identity != self.payload.dense_provider_identity:
            raise ValueError("Dense provider identity does not match the payload.")
        if self.dense.model_identity != self.payload.dense_model_identity:
            raise ValueError("Dense model identity does not match the payload.")
        if self.dense.config_checksum_sha256 != self.payload.dense_config_checksum_sha256:
            raise ValueError("Dense configuration identity does not match the payload.")
        if self.sparse.provider_identity != self.payload.sparse_provider_identity:
            raise ValueError("Sparse provider identity does not match the payload.")
        if self.sparse.model_identity != self.payload.sparse_model_identity:
            raise ValueError("Sparse model identity does not match the payload.")
        if self.sparse.config_checksum_sha256 != self.payload.sparse_config_checksum_sha256:
            raise ValueError("Sparse configuration identity does not match the payload.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return the Qdrant-shaped point without a vendor SDK dependency."""

        return {
            "id": self.point_id,
            "vector": {
                "dense": list(self.dense.vector),
                "sparse": {
                    "indices": list(self.sparse.indices),
                    "values": list(self.sparse.values),
                },
            },
            "payload": self.payload.canonical_payload(),
        }

    def canonical_bytes(self) -> bytes:
        """Serialize the exact immutable point used for idempotency checks."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the checksum used by a remote adapter for read-before-write."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class VectorPointRequest:
    """Inputs needed to project one validated chunk into a hybrid point."""

    tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk: Chunk
    metadata: DocumentMetadata
    pipeline_version: str
    collection: VectorCollectionConfig
    dense: DenseEmbedding
    sparse: SparseEmbedding

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise VectorIndexInputError("Vector requests require a UUID tenant id.")
        if not isinstance(self.document_id, UUID) or not isinstance(self.document_version_id, UUID):
            raise VectorIndexInputError("Vector requests require UUID document lineage.")
        if not isinstance(self.chunk, Chunk):
            raise VectorIndexInputError("Vector requests require a validated Chunk.")
        if not isinstance(self.metadata, DocumentMetadata):
            raise VectorIndexInputError("Vector requests require DocumentMetadata.")
        if not isinstance(self.collection, VectorCollectionConfig):
            raise VectorIndexInputError("Vector requests require VectorCollectionConfig.")
        if not isinstance(self.dense, DenseEmbedding) or not isinstance(
            self.sparse, SparseEmbedding
        ):
            raise VectorIndexInputError("Vector requests require dense and sparse embeddings.")
        if not isinstance(self.pipeline_version, str) or not self.pipeline_version.strip():
            raise VectorIndexInputError("Vector pipeline version cannot be empty.")
        if self.metadata.source_artifact_checksum != self.chunk.source_artifact_checksum:
            raise VectorIndexInputError("Metadata and chunk source checksums do not match.")

    def build_point(self) -> VectorPoint:
        """Validate representation geometry and build a deterministic point."""

        if self.dense.dimensions != self.collection.dense_dimensions:
            raise VectorIndexInputError("Dense dimensions do not match the collection geometry.")
        if self.dense.distance_metric != self.collection.dense_distance_metric:
            raise VectorIndexInputError("Dense distance does not match the collection geometry.")
        if self.sparse.index_space_size != self.collection.sparse_index_space_size:
            raise VectorIndexInputError(
                "Sparse index space does not match the collection geometry."
            )
        if self.sparse.distance_metric != self.collection.sparse_distance_metric:
            raise VectorIndexInputError("Sparse distance does not match the collection geometry.")
        if self.dense.input_checksum_sha256 != self.chunk.content_checksum_sha256:
            raise VectorIndexInputError("Dense vector input does not match the chunk checksum.")
        if self.sparse.input_checksum_sha256 != self.chunk.content_checksum_sha256:
            raise VectorIndexInputError("Sparse vector input does not match the chunk checksum.")

        collection_checksum = self.collection.checksum_sha256
        payload = VectorPointPayload(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            chunk_id=self.chunk.chunk_id,
            chunk_kind=self.chunk.chunk_kind,
            parent_chunk_id=self.chunk.parent_chunk_id,
            section_path=self.chunk.section_path,
            title=self.metadata.title,
            source_type=self.metadata.source_type,
            page_start=self.chunk.page_start,
            page_end=self.chunk.page_end,
            language=self.metadata.language,
            pipeline_version=self.pipeline_version,
            source_artifact_checksum=self.chunk.source_artifact_checksum,
            content_checksum_sha256=self.chunk.content_checksum_sha256,
            collection_config_checksum_sha256=collection_checksum,
            dense_dimensions=self.dense.dimensions,
            sparse_index_space_size=self.sparse.index_space_size,
            sparse_token_count=self.sparse.token_count,
            dense_provider_identity=self.dense.provider_identity,
            dense_model_identity=self.dense.model_identity,
            dense_config_checksum_sha256=self.dense.config_checksum_sha256,
            sparse_provider_identity=self.sparse.provider_identity,
            sparse_model_identity=self.sparse.model_identity,
            sparse_config_checksum_sha256=self.sparse.config_checksum_sha256,
        )
        point_id = _point_id(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            chunk=self.chunk,
            collection_checksum=collection_checksum,
            dense=self.dense,
            sparse=self.sparse,
        )
        return VectorPoint(
            point_id=point_id,
            collection_config_checksum_sha256=collection_checksum,
            dense=self.dense,
            sparse=self.sparse,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class TenantVectorFilter:
    """Required equality filter for every tenant-scoped vector operation."""

    tenant_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise VectorIndexInputError("Tenant filters require a UUID tenant id.")

    def to_qdrant_filter(self) -> dict[str, object]:
        """Return the provider-neutral JSON shape for a Qdrant tenant filter."""

        return {
            "must": [
                {
                    "key": "tenant_id",
                    "match": {"value": str(self.tenant_id)},
                }
            ]
        }


@dataclass(frozen=True, slots=True)
class VectorUpsertResult:
    """Outcome of a duplicate-safe vector point upsert."""

    point_id: str
    status: VectorUpsertStatus


class VectorIndex(Protocol):
    """Application-owned port implemented by Qdrant or a test adapter."""

    async def upsert(self, point: VectorPoint) -> VectorUpsertResult:
        """Create or reuse one immutable point."""

    async def get(self, *, point_id: str, tenant_id: UUID) -> VectorPoint | None:
        """Read one point only when its tenant matches the required filter."""


class InMemoryVectorIndex:
    """Small deterministic adapter used to prove the index-port semantics."""

    def __init__(self) -> None:
        self._points: dict[str, VectorPoint] = {}

    @property
    def points(self) -> tuple[VectorPoint, ...]:
        """Expose a stable snapshot for contract and pipeline verification."""

        return tuple(self._points[point_id] for point_id in sorted(self._points))

    async def upsert(self, point: VectorPoint) -> VectorUpsertResult:
        """Create a point, reuse identical bytes, or reject an immutable conflict."""

        if not isinstance(point, VectorPoint):
            raise VectorIndexInputError("Vector indexes require a VectorPoint.")
        existing = self._points.get(point.point_id)
        if existing is None:
            self._points[point.point_id] = point
            return VectorUpsertResult(point_id=point.point_id, status="created")
        if existing.canonical_bytes() != point.canonical_bytes():
            raise VectorPointConflictError(point_id=point.point_id)
        return VectorUpsertResult(point_id=point.point_id, status="reused")

    async def get(self, *, point_id: str, tenant_id: UUID) -> VectorPoint | None:
        """Return no data for a missing or foreign-tenant point."""

        if not isinstance(point_id, str) or not _POINT_ID_PATTERN.fullmatch(point_id):
            raise VectorIndexInputError("Vector reads require a valid point id.")
        if not isinstance(tenant_id, UUID):
            raise VectorIndexInputError("Vector reads require a UUID tenant id.")
        point = self._points.get(point_id)
        if point is None or point.payload.tenant_id != tenant_id:
            return None
        return point


def _point_id(
    *,
    tenant_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    chunk: Chunk,
    collection_checksum: str,
    dense: DenseEmbedding,
    sparse: SparseEmbedding,
) -> str:
    identity = {
        "tenant_id": str(tenant_id),
        "document_id": str(document_id),
        "document_version_id": str(document_version_id),
        "chunk_id": chunk.chunk_id,
        "content_checksum_sha256": chunk.content_checksum_sha256,
        "collection_config_checksum_sha256": collection_checksum,
        "dense_provider_identity": dense.provider_identity,
        "dense_model_identity": dense.model_identity,
        "dense_config_checksum_sha256": dense.config_checksum_sha256,
        "sparse_provider_identity": sparse.provider_identity,
        "sparse_model_identity": sparse.model_identity,
        "sparse_config_checksum_sha256": sparse.config_checksum_sha256,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return str(UUID(bytes=hashlib.sha256(canonical).digest()[:16], version=5))


__all__ = [
    "InMemoryVectorIndex",
    "TenantVectorFilter",
    "VECTOR_COLLECTION_SCHEMA_VERSION",
    "VECTOR_POINT_SCHEMA_VERSION",
    "VectorCollectionConfig",
    "VectorIndex",
    "VectorIndexConfigurationError",
    "VectorIndexDependencyError",
    "VectorIndexError",
    "VectorIndexInputError",
    "VectorPoint",
    "VectorPointConflictError",
    "VectorPointPayload",
    "VectorPointRequest",
    "VectorUpsertResult",
]
