"""Proof that canonical normalized artifacts can rebuild vector projections."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.chunking import ChunkingConfig
from openwikirag.application.embedding_batch import EmbeddingBatchConfig
from openwikirag.application.embeddings import EmbeddingConfig
from openwikirag.application.extraction import (
    InvalidProvenanceError,
    PlainTextExtractor,
    read_normalized_document,
)
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.normalized_artifacts import NormalizedArtifactService
from openwikirag.application.sparse import SparseEmbeddingConfig
from openwikirag.application.vector_index import InMemoryVectorIndex, VectorCollectionConfig
from openwikirag.application.vector_ingestion import VectorIngestionConfig, VectorIngestionService
from openwikirag.application.vector_rebuild import (
    VectorProjectionRebuildError,
    rebuild_vector_projection,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    Document,
    DocumentVersion,
    NormalizedDocumentArtifact,
    Tenant,
)
from openwikirag.infrastructure.qdrant import QdrantCollectionConfig, QdrantVectorIndex
from openwikirag.infrastructure.storage import LocalObjectStorage


@pytest.fixture
async def rebuild_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncSession, LocalObjectStorage, UUID, VectorIngestionConfig]]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'vector-rebuild.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    storage = LocalObjectStorage(tmp_path / "objects")
    async with create_session_factory(engine)() as session:
        tenant = Tenant(name=f"Rebuild Tenant {uuid4().hex}")
        session.add(tenant)
        await session.flush()
        document = Document(tenant_id=tenant.id, title="Rebuild Source", source_type="text")
        session.add(document)
        await session.flush()
        source = b"Canonical recovery keeps the normalized artifact and its provenance."
        source_key = f"tenants/{tenant.id}/documents/{document.id}/source"
        version = DocumentVersion(
            tenant_id=tenant.id,
            document_id=document.id,
            version_number=1,
            original_filename="source.txt",
            sanitized_filename="source.txt",
            source_type="text",
            media_type="text/plain",
            byte_size=len(source),
            checksum_sha256="a" * 64,
            source_object_key=source_key,
            pipeline_version="recovery-test-v1",
        )
        session.add(version)
        await session.flush()
        document.current_version_id = version.id
        await storage.put(object_key=source_key, data=source, content_type="text/plain")
        await session.commit()
        await NormalizedArtifactService(session, storage).persist(
            tenant_id=tenant.id,
            document_version_id=version.id,
        )
        config = VectorIngestionConfig(
            chunking=ChunkingConfig(
                parent_max_tokens=20,
                parent_max_characters=180,
                child_max_tokens=6,
                child_max_characters=80,
                child_overlap_tokens=1,
            ),
            dense=EmbeddingConfig(model_identity="recovery-dense-v1", dimensions=8),
            sparse=SparseEmbeddingConfig(
                model_identity="recovery-sparse-v1",
                index_space_size=64,
            ),
            collection=VectorCollectionConfig(
                collection_name=f"recovery-{uuid4().hex[:16]}",
                dense_dimensions=8,
                sparse_index_space_size=64,
            ),
            batch=EmbeddingBatchConfig(max_batch_size=3, max_concurrency=2),
        )
        yield session, storage, tenant.id, config
    await engine.dispose()


async def test_replay_rebuilds_points_from_verified_canonical_object(
    rebuild_context: tuple[AsyncSession, LocalObjectStorage, UUID, VectorIngestionConfig],
) -> None:
    session, storage, tenant_id, config = rebuild_context
    normalized = await session.scalar(select(NormalizedDocumentArtifact))
    assert normalized is not None
    version = await session.get(DocumentVersion, normalized.document_version_id)
    assert version is not None

    source_document = read_normalized_document(
        await storage.get(object_key=normalized.artifact_object_key)
    )
    first_index = InMemoryVectorIndex()
    first = await VectorIngestionService(
        session, storage, first_index, config=config
    ).project(
        tenant_id=tenant_id,
        document_id=version.document_id,
        document_version_id=normalized.document_version_id,
        normalized_artifact_id=normalized.id,
        document=source_document,
        metadata=DeterministicMetadataExtractor().extract(document=source_document),
        pipeline_version="recovery-test-v1",
    )

    rebuilt_index = InMemoryVectorIndex()
    result = await rebuild_vector_projection(
        tenant_id=tenant_id,
        session=session,
        storage=storage,
        vector_index=rebuilt_index,
        config=config,
    )

    assert result.normalized_artifacts == 1
    assert result.manifests_reused == 1
    assert result.points_created == first.point_count
    assert result.points_reused == 0
    assert len(rebuilt_index.points) == first.point_count
    assert {point.point_id for point in rebuilt_index.points} == {
        point.point_id for point in first_index.points
    }


async def test_replay_rejects_tampered_normalized_object(
    rebuild_context: tuple[AsyncSession, LocalObjectStorage, UUID, VectorIngestionConfig],
) -> None:
    session, storage, tenant_id, config = rebuild_context
    normalized = await session.scalar(select(NormalizedDocumentArtifact))
    assert normalized is not None
    await storage.put(
        object_key=normalized.artifact_object_key,
        data=b"tampered",
        content_type="application/octet-stream",
    )

    with pytest.raises(VectorProjectionRebuildError, match="checksum"):
        await rebuild_vector_projection(
            tenant_id=tenant_id,
            session=session,
            storage=storage,
            vector_index=InMemoryVectorIndex(),
            config=config,
        )


async def test_replay_rebuilds_live_qdrant_when_configured(
    rebuild_context: tuple[AsyncSession, LocalObjectStorage, UUID, VectorIngestionConfig],
) -> None:
    url = os.getenv("OPENWIKIRAG_TEST_QDRANT_URL")
    if not url:
        pytest.skip("Set OPENWIKIRAG_TEST_QDRANT_URL for a disposable Qdrant replay.")
    session, storage, tenant_id, config = rebuild_context
    client = AsyncQdrantClient(url=url)
    index = QdrantVectorIndex(
        client,
        config=QdrantCollectionConfig(vector=config.collection),
    )
    try:
        await index.ensure_schema()
        result = await rebuild_vector_projection(
            tenant_id=tenant_id,
            session=session,
            storage=storage,
            vector_index=index,
            config=config,
        )
        assert result.normalized_artifacts == 1
        assert result.points_reused == 0
        points = await client.count(collection_name=config.collection.collection_name, exact=True)
        assert points.count == result.points_created
    finally:
        await client.delete_collection(collection_name=config.collection.collection_name)
        await index.close()


def test_normalized_artifact_reader_requires_canonical_bytes() -> None:
    document = PlainTextExtractor().extract(b"replayable source")
    assert read_normalized_document(document.canonical_bytes()) == document
    with pytest.raises(InvalidProvenanceError, match="invalid"):
        read_normalized_document(b"not-json")
