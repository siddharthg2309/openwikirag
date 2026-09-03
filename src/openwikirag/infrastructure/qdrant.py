"""Qdrant adapter for the provider-neutral hybrid vector-index port."""

import asyncio
import hashlib
import json
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient, models

from openwikirag.application.embeddings import DenseEmbedding
from openwikirag.application.sparse import SparseEmbedding
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorIndex,
    VectorIndexError,
    VectorIndexInputError,
    VectorPoint,
    VectorPointConflictError,
    VectorPointPayload,
    VectorUpsertResult,
)
from openwikirag.core.config import Settings

QDRANT_ADAPTER_SCHEMA_VERSION: Literal["qdrant-adapter-v1"] = "qdrant-adapter-v1"
QdrantPayloadFieldType = Literal["keyword", "uuid", "integer"]

_POINT_CHECKSUM_FIELD = "openwikirag_point_checksum_sha256"
_PAYLOAD_INDEX_FIELDS = frozenset(
    {
        "tenant_id",
        "document_id",
        "document_version_id",
        "chunk_id",
        "chunk_kind",
        "source_type",
        "language",
        "pipeline_version",
        "page_start",
        "page_end",
    }
)


class QdrantAdapterError(VectorIndexError):
    """Base error for Qdrant dependency and schema failures."""


class QdrantDependencyError(QdrantAdapterError):
    """Raised when a Qdrant operation cannot complete."""

    def __init__(self, *, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(f"Qdrant {operation} failed: {message}")


class QdrantSchemaConflictError(QdrantAdapterError):
    """Raised when the existing collection does not match server-owned schema."""


class QdrantDataIntegrityError(QdrantAdapterError):
    """Raised when a stored Qdrant point cannot reconstruct its contract."""


@dataclass(frozen=True, slots=True)
class QdrantPayloadIndex:
    """One indexed payload field required by filtered retrieval."""

    field_name: str
    field_type: QdrantPayloadFieldType

    def __post_init__(self) -> None:
        if self.field_name not in _PAYLOAD_INDEX_FIELDS:
            raise QdrantSchemaConflictError(
                f"Unsupported Qdrant payload index field: {self.field_name}."
            )
        if self.field_type not in {"keyword", "uuid", "integer"}:
            raise QdrantSchemaConflictError(
                f"Unsupported Qdrant payload index type: {self.field_type}."
            )


DEFAULT_QDRANT_PAYLOAD_INDEXES: tuple[QdrantPayloadIndex, ...] = (
    QdrantPayloadIndex("tenant_id", "uuid"),
    QdrantPayloadIndex("document_id", "uuid"),
    QdrantPayloadIndex("document_version_id", "uuid"),
    QdrantPayloadIndex("chunk_id", "keyword"),
    QdrantPayloadIndex("chunk_kind", "keyword"),
    QdrantPayloadIndex("source_type", "keyword"),
    QdrantPayloadIndex("language", "keyword"),
    QdrantPayloadIndex("pipeline_version", "keyword"),
    QdrantPayloadIndex("page_start", "integer"),
    QdrantPayloadIndex("page_end", "integer"),
)


@dataclass(frozen=True, slots=True)
class QdrantCollectionConfig:
    """Qdrant-specific schema settings around the application collection contract."""

    vector: VectorCollectionConfig = field(default_factory=VectorCollectionConfig)
    payload_indexes: tuple[QdrantPayloadIndex, ...] = DEFAULT_QDRANT_PAYLOAD_INDEXES

    def __post_init__(self) -> None:
        field_names = tuple(index.field_name for index in self.payload_indexes)
        if len(field_names) != len(set(field_names)):
            raise QdrantSchemaConflictError("Qdrant payload index fields must be unique.")
        if "tenant_id" not in field_names:
            raise QdrantSchemaConflictError("Qdrant schema must index tenant_id.")


class QdrantVectorIndex(VectorIndex):
    """Async Qdrant adapter implementing tenant-safe hybrid point operations."""

    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        config: QdrantCollectionConfig | None = None,
    ) -> None:
        self._client = client
        self._config = config or QdrantCollectionConfig()
        self._write_lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        config: QdrantCollectionConfig | None = None,
    ) -> "QdrantVectorIndex":
        """Create an adapter from application-owned Qdrant settings."""

        client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        return cls(client, config=config)

    @property
    def config(self) -> QdrantCollectionConfig:
        """Return the server-owned collection configuration."""

        return self._config

    async def ensure_schema(self) -> None:
        """Create or validate the collection and all filtered payload indexes."""

        name = self._config.vector.collection_name
        try:
            exists = await self._client.collection_exists(collection_name=name)
            if not exists:
                await self._client.create_collection(
                    collection_name=name,
                    vectors_config={
                        "dense": models.VectorParams(
                            size=self._config.vector.dense_dimensions,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={"sparse": models.SparseVectorParams()},
                    on_disk_payload=True,
                )
            info = await self._client.get_collection(collection_name=name)
            _validate_collection_schema(info, self._config.vector)
            for index in self._config.payload_indexes:
                existing = info.payload_schema.get(index.field_name)
                expected = models.PayloadSchemaType(index.field_type)
                if existing is not None:
                    if existing.data_type != expected:
                        raise QdrantSchemaConflictError(
                            f"Payload index {index.field_name} has type "
                            f"{existing.data_type}, expected {expected}."
                        )
                    continue
                await self._client.create_payload_index(
                    collection_name=name,
                    field_name=index.field_name,
                    field_schema=expected,
                    wait=True,
                )
        except QdrantSchemaConflictError:
            raise
        except Exception as exc:
            raise QdrantDependencyError(operation="schema provisioning", message=str(exc)) from exc

    async def upsert(self, point: VectorPoint) -> VectorUpsertResult:
        """Create a point, reuse the same checksum, or reject a conflict."""

        self._validate_point_for_collection(point)
        async with self._write_lock:
            existing = await self._retrieve(point.point_id, with_vectors=True)
            if existing is not None:
                existing_point = _from_qdrant_record(
                    existing,
                    expected_collection=self._config.vector,
                )
                if _qdrant_storage_checksum(existing_point) == _qdrant_storage_checksum(point):
                    return VectorUpsertResult(point_id=point.point_id, status="reused")
                raise VectorPointConflictError(point_id=point.point_id)
            try:
                await self._client.upsert(
                    collection_name=self._config.vector.collection_name,
                    points=[_to_qdrant_point(point)],
                    wait=True,
                )
            except Exception as exc:
                raise QdrantDependencyError(operation="upsert", message=str(exc)) from exc
            return VectorUpsertResult(point_id=point.point_id, status="created")

    async def get(self, *, point_id: str, tenant_id: UUID) -> VectorPoint | None:
        """Read and reconstruct a point only for its owning tenant."""

        _validate_point_id(point_id)
        if not isinstance(tenant_id, UUID):
            raise VectorIndexInputError("Qdrant reads require a UUID tenant id.")
        record = await self._retrieve(point_id, with_vectors=True)
        if record is None or record.payload is None:
            return None
        raw_payload = cast(dict[str, object], dict(record.payload))
        if raw_payload.get("tenant_id") != str(tenant_id):
            return None
        return _from_qdrant_record(record, expected_collection=self._config.vector)

    async def close(self) -> None:
        """Close the owned asynchronous Qdrant client."""

        await self._client.close()

    def _validate_point_for_collection(self, point: VectorPoint) -> None:
        if not isinstance(point, VectorPoint):
            raise VectorIndexInputError("Qdrant indexes require a VectorPoint.")
        if (
            point.collection_config_checksum_sha256
            != self._config.vector.checksum_sha256
        ):
            raise QdrantSchemaConflictError(
                "Vector point configuration does not match the Qdrant collection."
            )
        _validate_point_id(point.point_id)

    async def _retrieve(self, point_id: str, *, with_vectors: bool) -> models.Record | None:
        try:
            records = await self._client.retrieve(
                collection_name=self._config.vector.collection_name,
                ids=[point_id],
                with_payload=True,
                with_vectors=with_vectors,
            )
        except Exception as exc:
            raise QdrantDependencyError(operation="retrieve", message=str(exc)) from exc
        return records[0] if records else None


def _validate_collection_schema(info: object, config: VectorCollectionConfig) -> None:
    """Reject an existing collection whose named geometry differs."""

    collection_config = getattr(info, "config", None)
    params = getattr(collection_config, "params", None)
    vectors = getattr(params, "vectors", None)
    sparse_vectors = getattr(params, "sparse_vectors", None)
    if not isinstance(vectors, dict) or set(vectors) != {"dense"}:
        raise QdrantSchemaConflictError("Qdrant dense vector schema is not exactly named dense.")
    dense = vectors.get("dense")
    if not isinstance(dense, models.VectorParams):
        raise QdrantSchemaConflictError("Qdrant dense vector configuration is malformed.")
    if dense.size != config.dense_dimensions or dense.distance != models.Distance.COSINE:
        raise QdrantSchemaConflictError("Qdrant dense geometry does not match configuration.")
    if not isinstance(sparse_vectors, dict) or set(sparse_vectors) != {"sparse"}:
        raise QdrantSchemaConflictError("Qdrant sparse vector schema is not exactly named sparse.")
    if not isinstance(sparse_vectors["sparse"], models.SparseVectorParams):
        raise QdrantSchemaConflictError("Qdrant sparse vector configuration is malformed.")


def _to_qdrant_point(point: VectorPoint) -> models.PointStruct:
    payload: dict[str, Any] = dict(point.payload.canonical_payload())
    payload[_POINT_CHECKSUM_FIELD] = _qdrant_storage_checksum(point)
    return models.PointStruct(
        id=point.point_id,
        vector={
            "dense": list(point.dense.vector),
            "sparse": models.SparseVector(
                indices=list(point.sparse.indices),
                values=list(point.sparse.values),
            ),
        },
        payload=payload,
    )


def _from_qdrant_record(
    record: models.Record,
    *,
    expected_collection: VectorCollectionConfig,
) -> VectorPoint:
    """Reconstruct and integrity-check an application point from Qdrant data."""

    if record.payload is None or not isinstance(record.vector, dict):
        raise QdrantDataIntegrityError("Qdrant record is missing payload or named vectors.")
    payload_data = cast(dict[str, object], dict(record.payload))
    stored_checksum = payload_data.pop(_POINT_CHECKSUM_FIELD, None)
    raw_vectors = cast(dict[str, object], dict(record.vector))
    dense_raw = raw_vectors.get("dense")
    sparse_raw = raw_vectors.get("sparse")
    if not isinstance(dense_raw, list):
        raise QdrantDataIntegrityError("Qdrant record is missing a dense vector.")

    try:
        payload = _validate_payload(payload_data)
        dense = DenseEmbedding(
            provider_identity=payload.dense_provider_identity,
            model_identity=payload.dense_model_identity,
            config_checksum_sha256=payload.dense_config_checksum_sha256,
            input_checksum_sha256=payload.content_checksum_sha256,
            dimensions=payload.dense_dimensions,
            distance_metric="cosine",
            vector=tuple(float(value) for value in dense_raw),
        )
        indices, values = _sparse_pairs(sparse_raw)
        sparse = SparseEmbedding(
            provider_identity=payload.sparse_provider_identity,
            model_identity=payload.sparse_model_identity,
            config_checksum_sha256=payload.sparse_config_checksum_sha256,
            input_checksum_sha256=payload.content_checksum_sha256,
            index_space_size=payload.sparse_index_space_size,
            distance_metric="dot",
            token_count=payload.sparse_token_count,
            index_count=len(indices),
            indices=indices,
            values=values,
        )
        point = VectorPoint(
            point_id=str(record.id),
            collection_config_checksum_sha256=expected_collection.checksum_sha256,
            dense=dense,
            sparse=sparse,
            payload=payload,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise QdrantDataIntegrityError(
            "Qdrant record violates the application point contract."
        ) from exc
    if stored_checksum != _qdrant_storage_checksum(point):
        raise QdrantDataIntegrityError("Qdrant point checksum does not match its contents.")
    return point


def _validate_payload(payload_data: dict[str, object]) -> VectorPointPayload:
    return VectorPointPayload.model_validate(payload_data)


def _sparse_pairs(sparse_raw: object) -> tuple[tuple[int, ...], tuple[float, ...]]:
    if isinstance(sparse_raw, models.SparseVector):
        return tuple(sparse_raw.indices), tuple(sparse_raw.values)
    if isinstance(sparse_raw, Mapping):
        indices = sparse_raw.get("indices")
        values = sparse_raw.get("values")
        if isinstance(indices, list) and isinstance(values, list):
            return tuple(int(index) for index in indices), tuple(float(value) for value in values)
    raise QdrantDataIntegrityError("Qdrant record sparse vector is malformed.")


def _validate_point_id(point_id: str) -> None:
    try:
        parsed = UUID(point_id)
    except (AttributeError, ValueError) as exc:
        raise VectorIndexInputError("Qdrant point ids must be canonical UUIDs.") from exc
    if str(parsed) != point_id:
        raise VectorIndexInputError("Qdrant point ids must be lowercase canonical UUIDs.")


def _qdrant_storage_checksum(point: VectorPoint) -> str:
    """Hash the representation after Qdrant's dense float32 storage conversion."""

    payload = {
        "id": point.point_id,
        "vector": {
            "dense": _qdrant_dense_values(point.dense.vector),
            "sparse": {
                "indices": list(point.sparse.indices),
                "values": [_float32(value) for value in point.sparse.values],
            },
        },
        "payload": point.payload.canonical_payload(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _float32(value: float) -> float:
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def _qdrant_dense_values(values: tuple[float, ...]) -> list[float]:
    """Match Qdrant cosine storage: float32 conversion followed by normalization."""

    converted = [_float32(value) for value in values]
    norm = math.sqrt(sum(value * value for value in converted))
    return [_float32(value / norm) for value in converted]


__all__ = [
    "DEFAULT_QDRANT_PAYLOAD_INDEXES",
    "QDRANT_ADAPTER_SCHEMA_VERSION",
    "QdrantAdapterError",
    "QdrantCollectionConfig",
    "QdrantDataIntegrityError",
    "QdrantDependencyError",
    "QdrantPayloadFieldType",
    "QdrantPayloadIndex",
    "QdrantSchemaConflictError",
    "QdrantVectorIndex",
]
