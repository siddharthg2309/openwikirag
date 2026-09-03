"""Proof for the provider-neutral tenant-scoped vector projection contract."""

import hashlib
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from openwikirag.application.chunking import Chunk
from openwikirag.application.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
)
from openwikirag.application.metadata import DocumentMetadata
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbeddingConfig,
    SparseEmbeddingRequest,
)
from openwikirag.application.vector_index import (
    InMemoryVectorIndex,
    TenantVectorFilter,
    VectorCollectionConfig,
    VectorIndexConfigurationError,
    VectorIndexInputError,
    VectorPoint,
    VectorPointConflictError,
    VectorPointRequest,
)

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
FOREIGN_TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")
DOCUMENT_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-4444-444444444444")


def _chunk(text: str = "JWT refresh tokens rotate after authentication.") -> Chunk:
    return Chunk(
        chunk_id=f"chunk-{hashlib.sha256(text.encode()).hexdigest()}",
        chunk_kind="parent",
        source_artifact_checksum="a" * 64,
        text=text,
        normalized_start_char=0,
        normalized_end_char=len(text),
        section_path=("Authentication",),
        page_start=4,
        page_end=5,
        token_count=len(text.split()),
        character_count=len(text),
        content_checksum_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


async def _request(
    *,
    tenant_id: UUID = TENANT_ID,
    document_version_id: UUID = VERSION_ID,
    text: str = "JWT refresh tokens rotate after authentication.",
) -> VectorPointRequest:
    chunk = _chunk(text)
    dense_config = EmbeddingConfig(model_identity="dense-test-v1", dimensions=8)
    sparse_config = SparseEmbeddingConfig(model_identity="sparse-test-v1", index_space_size=32)
    dense_request = EmbeddingRequest(
        text=text,
        input_checksum_sha256=chunk.content_checksum_sha256,
        config=dense_config,
    )
    sparse_request = SparseEmbeddingRequest(
        text=text,
        input_checksum_sha256=chunk.content_checksum_sha256,
        config=sparse_config,
    )
    dense = await DeterministicHashEmbeddingProvider().embed(dense_request)
    sparse = await DeterministicHashSparseEmbeddingProvider().embed(sparse_request)
    return VectorPointRequest(
        tenant_id=tenant_id,
        document_id=DOCUMENT_ID,
        document_version_id=document_version_id,
        chunk=chunk,
        metadata=DocumentMetadata(
            source_type="pdf",
            source_artifact_checksum=chunk.source_artifact_checksum,
            title="Authentication",
            language="en",
        ),
        pipeline_version="ingestion-v1",
        collection=VectorCollectionConfig(dense_dimensions=8, sparse_index_space_size=32),
        dense=dense,
        sparse=sparse,
    )


def test_collection_config_has_stable_identity_and_rejects_invalid_geometry() -> None:
    first = VectorCollectionConfig(dense_dimensions=8, sparse_index_space_size=32)
    second = VectorCollectionConfig(dense_dimensions=8, sparse_index_space_size=32)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    with pytest.raises(VectorIndexConfigurationError):
        VectorCollectionConfig(collection_name="not a valid collection")
    with pytest.raises(VectorIndexConfigurationError):
        VectorCollectionConfig(dense_dimensions=0)
    with pytest.raises(VectorIndexConfigurationError):
        VectorCollectionConfig(sparse_index_space_size=0)


async def test_projection_has_named_vectors_provenance_and_no_raw_text() -> None:
    point = (await _request()).build_point()
    projected = point.canonical_payload()
    vector = cast(dict[str, object], projected["vector"])
    payload = cast(dict[str, object], projected["payload"])

    assert projected["id"] == point.point_id
    assert set(vector) == {"dense", "sparse"}
    assert payload["tenant_id"] == str(TENANT_ID)
    assert payload["document_version_id"] == str(VERSION_ID)
    assert payload["source_type"] == "pdf"
    assert payload["page_start"] == 4
    assert payload["page_end"] == 5
    assert payload["pipeline_version"] == "ingestion-v1"
    assert payload["content_checksum_sha256"] == point.sparse.input_checksum_sha256
    assert "text" not in payload


async def test_projection_identity_is_deterministic_and_tenant_scoped() -> None:
    first = (await _request()).build_point()
    repeated = (await _request()).build_point()
    foreign = (await _request(tenant_id=FOREIGN_TENANT_ID)).build_point()
    changed_version = (await _request(document_version_id=DOCUMENT_ID)).build_point()

    assert first.point_id == repeated.point_id
    assert first.canonical_bytes() == repeated.canonical_bytes()
    assert first.point_id != foreign.point_id
    assert first.point_id != changed_version.point_id


async def test_projection_rejects_geometry_and_input_checksum_mismatches() -> None:
    request = await _request()
    with pytest.raises(VectorIndexInputError, match="Dense dimensions"):
        replace(
            request,
            collection=VectorCollectionConfig(dense_dimensions=4, sparse_index_space_size=32),
        ).build_point()
    with pytest.raises(VectorIndexInputError, match="Dense vector input"):
        replace(
            request,
            dense=request.dense.model_copy(update={"input_checksum_sha256": "b" * 64}),
        ).build_point()
    with pytest.raises(VectorIndexInputError, match="Sparse vector input"):
        replace(
            request,
            sparse=request.sparse.model_copy(update={"input_checksum_sha256": "c" * 64}),
        ).build_point()


def test_tenant_filter_serializes_to_explicit_qdrant_shape() -> None:
    assert TenantVectorFilter(tenant_id=TENANT_ID).to_qdrant_filter() == {
        "must": [
            {
                "key": "tenant_id",
                "match": {"value": str(TENANT_ID)},
            }
        ]
    }


async def test_index_creates_reuses_and_hides_foreign_tenant_points() -> None:
    point = (await _request()).build_point()
    index = InMemoryVectorIndex()

    created = await index.upsert(point)
    reused = await index.upsert(point)

    assert created.status == "created"
    assert reused.status == "reused"
    assert await index.get(point_id=point.point_id, tenant_id=TENANT_ID) == point
    assert await index.get(point_id=point.point_id, tenant_id=FOREIGN_TENANT_ID) is None


async def test_index_rejects_same_id_with_different_immutable_bytes() -> None:
    point = (await _request()).build_point()
    changed_dense = point.dense.model_copy(
        update={"vector": tuple(-value for value in point.dense.vector)}
    )
    changed = VectorPoint(
        point_id=point.point_id,
        collection_config_checksum_sha256=point.collection_config_checksum_sha256,
        dense=changed_dense,
        sparse=point.sparse,
        payload=point.payload,
    )
    index = InMemoryVectorIndex()
    await index.upsert(point)

    with pytest.raises(VectorPointConflictError):
        await index.upsert(changed)


async def test_index_rejects_non_point_and_invalid_point_reads() -> None:
    index = InMemoryVectorIndex()
    with pytest.raises(VectorIndexInputError):
        await index.upsert(object())  # type: ignore[arg-type]
    with pytest.raises(VectorIndexInputError):
        await index.get(point_id="invalid", tenant_id=TENANT_ID)


async def test_point_rejects_payload_identity_mismatch() -> None:
    point = (await _request()).build_point()
    mismatched_payload = point.payload.model_copy(
        update={"dense_config_checksum_sha256": "d" * 64}
    )

    with pytest.raises(ValidationError):
        VectorPoint(
            point_id=point.point_id,
            collection_config_checksum_sha256=point.collection_config_checksum_sha256,
            dense=point.dense,
            sparse=point.sparse,
            payload=mismatched_payload,
        )
