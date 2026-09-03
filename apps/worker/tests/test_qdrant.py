"""Proof for the Qdrant adapter with local and optional server integrations."""

import os
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient, models

from apps.worker.tests.test_vector_index import (
    FOREIGN_TENANT_ID,
    TENANT_ID,
    _request,
)
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorPoint,
    VectorPointConflictError,
)
from openwikirag.infrastructure.qdrant import (
    QdrantCollectionConfig,
    QdrantDataIntegrityError,
    QdrantSchemaConflictError,
    QdrantVectorIndex,
)


def _config() -> QdrantCollectionConfig:
    return QdrantCollectionConfig(
        vector=VectorCollectionConfig(
            collection_name=f"openwikirag-test-{uuid4().hex[:16]}",
            dense_dimensions=8,
            sparse_index_space_size=32,
        )
    )


def _assert_named_schema(info: models.CollectionInfo) -> None:
    vectors = info.config.params.vectors
    assert isinstance(vectors, dict)
    assert set(vectors) == {"dense"}
    assert vectors["dense"].size == 8
    assert vectors["dense"].distance == models.Distance.COSINE
    sparse_vectors = info.config.params.sparse_vectors
    assert isinstance(sparse_vectors, dict)
    assert set(sparse_vectors) == {"sparse"}


async def _local_adapter() -> tuple[QdrantVectorIndex, AsyncQdrantClient]:
    client = AsyncQdrantClient(":memory:")
    return QdrantVectorIndex(client, config=_config()), client


async def test_local_qdrant_provisions_named_schema_idempotently() -> None:
    adapter, client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        await adapter.ensure_schema()

        info = await client.get_collection(adapter.config.vector.collection_name)
        _assert_named_schema(info)
    finally:
        await adapter.close()


async def test_local_qdrant_round_trip_reconstructs_validated_point() -> None:
    adapter, _client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        point = (await _request(collection=adapter.config.vector)).build_point()

        result = await adapter.upsert(point)
        stored = await adapter.get(point_id=point.point_id, tenant_id=TENANT_ID)

        assert result.status == "created"
        assert stored is not None
        assert stored.point_id == point.point_id
        assert stored.payload == point.payload
        assert stored.dense.vector == pytest.approx(point.dense.vector, abs=1e-6)
        assert stored.sparse.indices == point.sparse.indices
        assert stored.sparse.values == pytest.approx(point.sparse.values, abs=1e-6)
    finally:
        await adapter.close()


async def test_local_qdrant_reuses_checksum_and_hides_foreign_tenant() -> None:
    adapter, _client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        point = (await _request(collection=adapter.config.vector)).build_point()

        await adapter.upsert(point)
        reused = await adapter.upsert(point)

        assert reused.status == "reused"
        assert await adapter.get(point_id=point.point_id, tenant_id=FOREIGN_TENANT_ID) is None
    finally:
        await adapter.close()


async def test_local_qdrant_rejects_same_id_with_different_checksum() -> None:
    adapter, _client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        point = (await _request(collection=adapter.config.vector)).build_point()
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
        await adapter.upsert(point)

        with pytest.raises(VectorPointConflictError):
            await adapter.upsert(changed)
    finally:
        await adapter.close()


async def test_existing_incompatible_collection_fails_closed() -> None:
    client = AsyncQdrantClient(":memory:")
    config = _config()
    try:
        await client.create_collection(
            collection_name=config.vector.collection_name,
            vectors_config={
                "dense": models.VectorParams(size=4, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        with pytest.raises(QdrantSchemaConflictError):
            await QdrantVectorIndex(client, config=config).ensure_schema()
    finally:
        await client.close()


async def test_corrupted_local_record_fails_closed() -> None:
    adapter, client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        point = (await _request(collection=adapter.config.vector)).build_point()
        await adapter.upsert(point)
        await client.set_payload(
            collection_name=adapter.config.vector.collection_name,
            payload={"openwikirag_point_checksum_sha256": "0" * 64},
            points=[point.point_id],
        )

        with pytest.raises(QdrantDataIntegrityError):
            await adapter.get(point_id=point.point_id, tenant_id=TENANT_ID)
    finally:
        await adapter.close()


@pytest.mark.skipif(
    not os.environ.get("OPENWIKIRAG_TEST_QDRANT_URL"),
    reason="Set OPENWIKIRAG_TEST_QDRANT_URL to run the real Qdrant integration.",
)
async def test_real_qdrant_schema_indexes_and_round_trip() -> None:
    url = os.environ["OPENWIKIRAG_TEST_QDRANT_URL"]
    client = AsyncQdrantClient(url=url)
    config = _config()
    adapter = QdrantVectorIndex(client, config=config)
    try:
        await adapter.ensure_schema()
        await adapter.ensure_schema()
        info = await client.get_collection(config.vector.collection_name)
        _assert_named_schema(info)
        assert set(info.payload_schema) == {
            index.field_name for index in config.payload_indexes
        }
        assert {
            field_name: payload_index.data_type.value
            for field_name, payload_index in info.payload_schema.items()
        } == {
            index.field_name: index.field_type
            for index in config.payload_indexes
        }

        point = (await _request(collection=config.vector)).build_point()
        assert (await adapter.upsert(point)).status == "created"
        assert (await adapter.upsert(point)).status == "reused"
        stored = await adapter.get(point_id=point.point_id, tenant_id=TENANT_ID)
        assert stored is not None
        assert stored.point_id == point.point_id
        assert stored.payload == point.payload
        assert stored.dense.vector == pytest.approx(point.dense.vector, abs=1e-6)
        assert stored.sparse.indices == point.sparse.indices
        assert stored.sparse.values == pytest.approx(point.sparse.values, abs=1e-6)
        assert await adapter.get(point_id=point.point_id, tenant_id=FOREIGN_TENANT_ID) is None
    finally:
        await client.delete_collection(collection_name=config.vector.collection_name)
        await adapter.close()
