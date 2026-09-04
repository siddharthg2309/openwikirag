"""Proof for canonical chunk-to-vector ingestion orchestration."""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.chunking import ChunkingConfig
from openwikirag.application.embedding_batch import EmbeddingBatchConfig
from openwikirag.application.embeddings import (
    DenseEmbedding,
    DenseEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
)
from openwikirag.application.metadata import DeterministicMetadataExtractor, DocumentMetadata
from openwikirag.application.normalized_artifacts import (
    NormalizedArtifactService,
    PersistedNormalizedArtifact,
)
from openwikirag.application.sparse import SparseEmbeddingConfig
from openwikirag.application.vector_index import (
    InMemoryVectorIndex,
    VectorCollectionConfig,
    VectorIndex,
    VectorIndexDependencyError,
    VectorPoint,
    VectorUpsertResult,
)
from openwikirag.application.vector_ingestion import (
    VectorIngestionConfig,
    VectorIngestionDependencyError,
    VectorIngestionInputError,
    VectorIngestionResult,
    VectorIngestionService,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    ChunkManifestArtifact,
    Document,
    DocumentVersion,
    Tenant,
)
from openwikirag.infrastructure.qdrant import QdrantCollectionConfig, QdrantVectorIndex
from openwikirag.infrastructure.storage import LocalObjectStorage

SOURCE_DATA = (
    b"# Authentication\nJWT signatures establish identity before authorization.\n"
    b"# Retrieval\nDense and sparse representations preserve complementary signals.\n"
)


@dataclass(slots=True)
class VectorIngestionContext:
    session: AsyncSession
    storage: LocalObjectStorage
    tenant_id: UUID
    foreign_tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    normalized: PersistedNormalizedArtifact
    metadata: DocumentMetadata
    config: VectorIngestionConfig


@pytest.fixture
async def vector_ingestion_context(
    tmp_path: Path,
) -> AsyncIterator[VectorIngestionContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'vector-ingestion.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    storage = LocalObjectStorage(tmp_path / "objects")
    async with session_factory() as session:
        tenant = Tenant(name=f"Vector Tenant {uuid4().hex}")
        foreign = Tenant(name=f"Foreign Vector Tenant {uuid4().hex}")
        session.add_all((tenant, foreign))
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            title="Vector Source",
            source_type="markdown",
        )
        session.add(document)
        await session.flush()
        object_key = f"tenants/{tenant.id}/documents/{document.id}/source"
        version = DocumentVersion(
            tenant_id=tenant.id,
            document_id=document.id,
            version_number=1,
            original_filename="source.md",
            sanitized_filename="source.md",
            source_type="markdown",
            media_type="text/markdown",
            byte_size=len(SOURCE_DATA),
            checksum_sha256="a" * 64,
            source_object_key=object_key,
            pipeline_version="ingestion-test-v1",
        )
        session.add(version)
        await session.flush()
        document.current_version_id = version.id
        await storage.put(
            object_key=object_key,
            data=SOURCE_DATA,
            content_type="text/markdown",
        )
        await session.commit()

        normalized = await NormalizedArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
        )
        metadata = DeterministicMetadataExtractor().extract(
            document=normalized.normalized_document
        )
        collection = VectorCollectionConfig(
            collection_name=f"openwikirag-ingestion-{uuid4().hex[:16]}",
            dense_dimensions=8,
            sparse_index_space_size=64,
        )
        config = VectorIngestionConfig(
            chunking=ChunkingConfig(
                parent_max_tokens=20,
                parent_max_characters=180,
                child_max_tokens=6,
                child_max_characters=80,
                child_overlap_tokens=1,
            ),
            dense=EmbeddingConfig(model_identity="dense-ingestion-v1", dimensions=8),
            sparse=SparseEmbeddingConfig(
                model_identity="sparse-ingestion-v1",
                index_space_size=64,
            ),
            collection=collection,
            batch=EmbeddingBatchConfig(max_batch_size=3, max_concurrency=2),
        )
        yield VectorIngestionContext(
            session=session,
            storage=storage,
            tenant_id=tenant.id,
            foreign_tenant_id=foreign.id,
            document_id=document.id,
            document_version_id=version.id,
            normalized=normalized,
            metadata=metadata,
            config=config,
        )
    await engine.dispose()


def _service(
    context: VectorIngestionContext,
    vector_index: VectorIndex,
    *,
    dense_provider: DenseEmbeddingProvider | None = None,
) -> VectorIngestionService:
    return VectorIngestionService(
        context.session,
        context.storage,
        vector_index,
        config=context.config,
        dense_provider=dense_provider,
    )


async def _project(
    context: VectorIngestionContext,
    service: VectorIngestionService,
) -> VectorIngestionResult:
    return await service.project(
        tenant_id=context.tenant_id,
        document_id=context.document_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized.artifact_id,
        document=context.normalized.normalized_document,
        metadata=context.metadata,
        pipeline_version=context.normalized.pipeline_version,
    )


async def test_projects_every_manifest_chunk_to_provenance_only_point(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    index = InMemoryVectorIndex()

    result = await _project(context, _service(context, index))

    artifact = await context.session.get(ChunkManifestArtifact, result.manifest_artifact_id)
    assert artifact is not None
    assert result.manifest_reused is False
    assert result.point_count == artifact.chunk_count == len(index.points)
    assert result.points_created == result.point_count
    assert result.points_reused == 0
    stored_manifest = json.loads(
        (await context.storage.get(object_key=artifact.artifact_object_key)).decode("utf-8")
    )
    assert stored_manifest["chunk_count"] == result.point_count
    assert all(point.payload.tenant_id == context.tenant_id for point in index.points)
    assert all(point.payload.document_id == context.document_id for point in index.points)
    assert all(
        point.payload.document_version_id == context.document_version_id
        for point in index.points
    )
    assert all(point.payload.pipeline_version == "ingestion-test-v1" for point in index.points)
    assert all("text" not in point.payload.canonical_payload() for point in index.points)


async def test_replay_reuses_manifest_and_every_point(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    index = InMemoryVectorIndex()
    service = _service(context, index)

    first = await _project(context, service)
    second = await _project(context, service)

    assert first.points_created == first.point_count
    assert second.manifest_artifact_id == first.manifest_artifact_id
    assert second.manifest_reused is True
    assert second.points_created == 0
    assert second.points_reused == first.point_count
    assert len(index.points) == first.point_count
    assert await context.session.scalar(select(func.count(ChunkManifestArtifact.id))) == 1


class FailAfterFirstPointIndex:
    def __init__(self) -> None:
        self.inner = InMemoryVectorIndex()
        self.calls = 0
        self.failed = False

    async def upsert(self, point: VectorPoint) -> VectorUpsertResult:
        self.calls += 1
        if self.calls == 2 and not self.failed:
            self.failed = True
            raise VectorIndexDependencyError("Qdrant unavailable")
        return await self.inner.upsert(point)

    async def get(self, *, point_id: str, tenant_id: UUID) -> VectorPoint | None:
        return await self.inner.get(point_id=point_id, tenant_id=tenant_id)


async def test_partial_projection_retry_reuses_prior_point_and_completes(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    index = FailAfterFirstPointIndex()
    service = _service(context, index)

    with pytest.raises(VectorIngestionDependencyError):
        await _project(context, service)
    assert len(index.inner.points) == 1

    completed = await _project(context, service)

    assert completed.manifest_reused is True
    assert completed.points_reused == 1
    assert completed.points_created == completed.point_count - 1
    assert len(index.inner.points) == completed.point_count


class FailingDenseProvider:
    provider_identity = "failing-dense-provider-v1"

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        del request
        raise RuntimeError("provider unavailable")


async def test_provider_failure_is_retryable_after_manifest_commit(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    index = InMemoryVectorIndex()

    with pytest.raises(VectorIngestionDependencyError):
        await _project(
            context,
            _service(context, index, dense_provider=FailingDenseProvider()),
        )

    assert await context.session.scalar(select(func.count(ChunkManifestArtifact.id))) == 1
    assert index.points == ()


async def test_mismatched_metadata_fails_before_manifest_or_vector_write(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    index = InMemoryVectorIndex()
    mismatched = context.metadata.model_copy(
        update={"source_artifact_checksum": "f" * 64}
    )

    with pytest.raises(VectorIngestionInputError):
        await _service(context, index).project(
            tenant_id=context.tenant_id,
            document_id=context.document_id,
            document_version_id=context.document_version_id,
            normalized_artifact_id=context.normalized.artifact_id,
            document=context.normalized.normalized_document,
            metadata=mismatched,
            pipeline_version=context.normalized.pipeline_version,
        )

    assert await context.session.scalar(select(func.count(ChunkManifestArtifact.id))) == 0
    assert index.points == ()


def test_configuration_rejects_embedding_collection_geometry_mismatch() -> None:
    with pytest.raises(VectorIngestionInputError, match="dimensions"):
        VectorIngestionConfig(
            dense=EmbeddingConfig(dimensions=8),
            collection=VectorCollectionConfig(dense_dimensions=16),
        )


@pytest.mark.skipif(
    not os.environ.get("OPENWIKIRAG_TEST_QDRANT_URL"),
    reason="Set OPENWIKIRAG_TEST_QDRANT_URL to run worker projection against Qdrant.",
)
async def test_real_qdrant_receives_every_worker_projection_point(
    vector_ingestion_context: VectorIngestionContext,
) -> None:
    context = vector_ingestion_context
    client = AsyncQdrantClient(url=os.environ["OPENWIKIRAG_TEST_QDRANT_URL"])
    adapter = QdrantVectorIndex(
        client,
        config=QdrantCollectionConfig(vector=context.config.collection),
    )
    try:
        await adapter.ensure_schema()
        service = _service(context, adapter)

        first = await _project(context, service)
        second = await _project(context, service)
        count = await client.count(
            collection_name=context.config.collection.collection_name,
            exact=True,
        )

        assert count.count == first.point_count
        assert second.manifest_reused is True
        assert second.points_created == 0
        assert second.points_reused == first.point_count
    finally:
        if await client.collection_exists(
            collection_name=context.config.collection.collection_name
        ):
            await client.delete_collection(
                collection_name=context.config.collection.collection_name
            )
        await adapter.close()
