"""Proof for the Qdrant adapter with local and optional server integrations."""

import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient, models

from apps.worker.tests.test_vector_index import (
    DOCUMENT_ID,
    FOREIGN_TENANT_ID,
    TENANT_ID,
    VERSION_ID,
    _request,
)
from openwikirag.application.embeddings import EmbeddingConfig
from openwikirag.application.retrieval import (
    CandidateRetrievalConfig,
    CandidateRetrievalService,
    SearchFilters,
    SearchRequest,
)
from openwikirag.application.sparse import SparseEmbeddingConfig
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorPoint,
    VectorPointConflictError,
)
from openwikirag.infrastructure.qdrant import (
    QdrantCollectionConfig,
    QdrantDataIntegrityError,
    QdrantDependencyError,
    QdrantSchemaConflictError,
    QdrantVectorIndex,
)

OTHER_VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")
EMPTY_TENANT_ID = UUID("66666666-6666-6666-6666-666666666666")
OTHER_DOCUMENT_ID = UUID("77777777-7777-7777-7777-777777777777")


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


def _retrieval_config(adapter: QdrantVectorIndex) -> CandidateRetrievalConfig:
    return CandidateRetrievalConfig(
        dense=EmbeddingConfig(model_identity="dense-test-v1", dimensions=8),
        sparse=SparseEmbeddingConfig(
            model_identity="sparse-test-v1",
            index_space_size=32,
        ),
        collection=adapter.config.vector,
    )


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


async def test_local_qdrant_searches_named_legs_with_tenant_and_optional_filters() -> None:
    adapter, _client = await _local_adapter()
    try:
        await adapter.ensure_schema()
        exact = (await _request(collection=adapter.config.vector)).build_point()
        other = (
            await _request(
                document_version_id=OTHER_VERSION_ID,
                text="Redis leases reclaim abandoned ingestion jobs.",
                collection=adapter.config.vector,
            )
        ).build_point()
        other_tenant = (
            await _request(
                tenant_id=EMPTY_TENANT_ID,
                collection=adapter.config.vector,
            )
        ).build_point()
        for point in (exact, other, other_tenant):
            await adapter.upsert(point)
        service = CandidateRetrievalService(
            adapter,
            config=_retrieval_config(adapter),
        )

        hybrid = await service.retrieve(
            SearchRequest(
                tenant_id=TENANT_ID,
                query="JWT refresh tokens rotate after authentication.",
            )
        )
        assert hybrid.dense_candidates[0].point_id == exact.point_id
        assert hybrid.sparse_candidates[0].point_id == exact.point_id
        assert all(
            candidate.payload.tenant_id == TENANT_ID
            for candidate in hybrid.dense_candidates + hybrid.sparse_candidates
        )

        filtered = await service.retrieve(
            SearchRequest(
                tenant_id=TENANT_ID,
                query="Redis leases",
                filters=SearchFilters(
                    document_ids=(OTHER_DOCUMENT_ID, DOCUMENT_ID),
                    document_version_ids=(OTHER_VERSION_ID,),
                    source_types=("pdf",),
                    languages=("en",),
                    chunk_kinds=("parent",),
                    pipeline_versions=("ingestion-v1",),
                ),
            )
        )
        candidates = filtered.dense_candidates + filtered.sparse_candidates
        assert candidates
        assert all(
            candidate.payload.document_version_id == OTHER_VERSION_ID
            for candidate in candidates
        )

        incompatible_service = CandidateRetrievalService(
            adapter,
            config=CandidateRetrievalConfig(
                dense=EmbeddingConfig(model_identity="other-dense-v1", dimensions=8),
                sparse=SparseEmbeddingConfig(
                    model_identity="other-sparse-v1",
                    index_space_size=32,
                ),
                collection=adapter.config.vector,
            ),
        )
        incompatible = await incompatible_service.retrieve(
            SearchRequest(
                tenant_id=TENANT_ID,
                query="JWT refresh tokens rotate after authentication.",
            )
        )
        assert incompatible.dense_candidates == ()
        assert incompatible.sparse_candidates == ()

        hidden = await service.retrieve(
            SearchRequest(
                tenant_id=FOREIGN_TENANT_ID,
                query="Redis leases",
                filters=SearchFilters(source_types=("pdf",)),
            )
        )
        assert hidden.dense_candidates == ()
        assert hidden.sparse_candidates == ()
    finally:
        await adapter.close()


async def test_qdrant_search_request_contains_named_vectors_and_all_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = await _local_adapter()
    captured: list[dict[str, Any]] = []

    async def record_query(**kwargs: Any) -> SimpleNamespace:
        captured.append(kwargs)
        return SimpleNamespace(points=[])

    monkeypatch.setattr(client, "query_points", record_query)
    request = await _request(collection=adapter.config.vector)
    filters = SearchFilters(
        document_ids=(DOCUMENT_ID, VERSION_ID),
        document_version_ids=(VERSION_ID,),
        source_types=("pdf", "markdown"),
        languages=("en",),
        chunk_kinds=("parent",),
        pipeline_versions=("ingestion-v1",),
    )
    try:
        await adapter.search_dense(
            tenant_id=TENANT_ID,
            query=request.dense,
            filters=filters,
            limit=7,
        )
        await adapter.search_sparse(
            tenant_id=TENANT_ID,
            query=request.sparse,
            filters=filters,
            limit=7,
        )
    finally:
        await adapter.close()

    assert len(captured) == 2
    assert captured[0]["using"] == "dense"
    assert isinstance(captured[0]["query"], list)
    assert captured[1]["using"] == "sparse"
    assert isinstance(captured[1]["query"], models.SparseVector)
    for call in captured:
        assert call["limit"] == 7
        assert call["with_payload"] is True
        assert call["with_vectors"] is False
        query_filter = call["query_filter"]
        assert isinstance(query_filter, models.Filter)
        serialized = json.dumps(
            query_filter.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
        )
        assert str(TENANT_ID) in serialized
        assert '"should"' in serialized
        assert '"any"' in serialized
    assert "dense_model_identity" in json.dumps(
        captured[0]["query_filter"].model_dump(mode="json", exclude_none=True)
    )
    assert "sparse_model_identity" in json.dumps(
        captured[1]["query_filter"].model_dump(mode="json", exclude_none=True)
    )


async def test_qdrant_search_rejects_geometry_before_client_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = await _local_adapter()
    calls = 0

    async def record_query(**kwargs: Any) -> SimpleNamespace:
        nonlocal calls
        del kwargs
        calls += 1
        return SimpleNamespace(points=[])

    monkeypatch.setattr(client, "query_points", record_query)
    query = (await _request(collection=adapter.config.vector)).dense.model_copy(
        update={"dimensions": 4}
    )
    try:
        with pytest.raises(QdrantSchemaConflictError, match="geometry"):
            await adapter.search_dense(
                tenant_id=TENANT_ID,
                query=query,
                filters=SearchFilters(),
                limit=5,
            )
        assert calls == 0
    finally:
        await adapter.close()


async def test_qdrant_search_distinguishes_dependency_and_integrity_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = await _local_adapter()
    query = (await _request(collection=adapter.config.vector)).dense

    async def fail_query(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(client, "query_points", fail_query)
    try:
        with pytest.raises(
            QdrantDependencyError,
            match="Qdrant dense candidate search failed",
        ):
            await adapter.search_dense(
                tenant_id=TENANT_ID,
                query=query,
                filters=SearchFilters(),
                limit=5,
            )
        async def malformed_query(**kwargs: Any) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(
                points=[models.ScoredPoint(id=1, version=0, score=1.0, payload={})]
            )

        monkeypatch.setattr(client, "query_points", malformed_query)
        with pytest.raises(QdrantDataIntegrityError):
            await adapter.search_dense(
                tenant_id=TENANT_ID,
                query=query,
                filters=SearchFilters(),
                limit=5,
            )
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
        assert len(info.payload_schema) == 16

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

        service = CandidateRetrievalService(
            adapter,
            config=_retrieval_config(adapter),
        )
        candidates = await service.retrieve(
            SearchRequest(
                tenant_id=TENANT_ID,
                query="JWT refresh tokens rotate after authentication.",
            )
        )
        assert candidates.dense_candidates[0].point_id == point.point_id
        assert candidates.sparse_candidates[0].point_id == point.point_id
        foreign = await service.retrieve(
            SearchRequest(
                tenant_id=FOREIGN_TENANT_ID,
                query="JWT refresh tokens rotate after authentication.",
            )
        )
        assert foreign.dense_candidates == ()
        assert foreign.sparse_candidates == ()
    finally:
        await client.delete_collection(collection_name=config.vector.collection_name)
        await adapter.close()
