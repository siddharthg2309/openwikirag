"""Compose canonical chunks, bounded embeddings, and vector projections."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.chunk_artifacts import (
    ChunkManifestError,
    ChunkManifestPersistenceError,
    ChunkManifestService,
    ChunkManifestStorageError,
)
from openwikirag.application.chunking import ChunkingConfig, ChunkingError, HierarchicalChunker
from openwikirag.application.embedding_batch import (
    EmbeddingBatchConfig,
    EmbeddingBatcher,
    EmbeddingBatchError,
    EmbeddingBatchProviderError,
)
from openwikirag.application.embeddings import (
    DenseEmbedding,
    DenseEmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
    validate_embedding_result,
)
from openwikirag.application.extraction import NormalizedDocument
from openwikirag.application.metadata import DocumentMetadata
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbedding,
    SparseEmbeddingConfig,
    SparseEmbeddingProvider,
    SparseEmbeddingRequest,
    validate_sparse_embedding_result,
)
from openwikirag.application.vector_index import (
    VectorCollectionConfig,
    VectorIndex,
    VectorIndexDependencyError,
    VectorIndexError,
    VectorPointRequest,
)
from openwikirag.infrastructure.storage import ObjectStorage


class VectorIngestionError(Exception):
    """Base error for chunk-to-vector orchestration."""


class VectorIngestionInputError(VectorIngestionError):
    """Raised when canonical input or deterministic output is invalid."""


class VectorIngestionDependencyError(VectorIngestionError):
    """Raised when storage, provider, metadata, or vector dependencies fail."""


def _default_sparse_config() -> SparseEmbeddingConfig:
    return SparseEmbeddingConfig(index_space_size=2**20)


@dataclass(frozen=True, slots=True)
class VectorIngestionConfig:
    """Server-owned configuration for one complete chunk projection run."""

    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    dense: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    sparse: SparseEmbeddingConfig = field(default_factory=_default_sparse_config)
    collection: VectorCollectionConfig = field(default_factory=VectorCollectionConfig)
    batch: EmbeddingBatchConfig = field(default_factory=EmbeddingBatchConfig)

    def __post_init__(self) -> None:
        if self.dense.dimensions != self.collection.dense_dimensions:
            raise VectorIngestionInputError(
                "Dense embedding dimensions must match the vector collection."
            )
        if self.dense.distance_metric != self.collection.dense_distance_metric:
            raise VectorIngestionInputError(
                "Dense embedding distance must match the vector collection."
            )
        if self.sparse.index_space_size != self.collection.sparse_index_space_size:
            raise VectorIngestionInputError(
                "Sparse embedding space must match the vector collection."
            )
        if self.sparse.distance_metric != self.collection.sparse_distance_metric:
            raise VectorIngestionInputError(
                "Sparse embedding distance must match the vector collection."
            )


@dataclass(frozen=True, slots=True)
class VectorIngestionResult:
    """Complete manifest and point outcome; partial success is never returned."""

    manifest_artifact_id: UUID
    manifest_reused: bool
    point_count: int
    points_created: int
    points_reused: int


class VectorIngestionService:
    """Persist canonical chunks, embed them, and write rebuildable points."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        vector_index: VectorIndex,
        *,
        config: VectorIngestionConfig | None = None,
        dense_provider: DenseEmbeddingProvider | None = None,
        sparse_provider: SparseEmbeddingProvider | None = None,
    ) -> None:
        self._config = config or VectorIngestionConfig()
        self._manifest = ChunkManifestService(session, storage)
        self._vector_index = vector_index
        self._dense_batcher = EmbeddingBatcher[EmbeddingRequest, DenseEmbedding](
            dense_provider or DeterministicHashEmbeddingProvider(),
            config=self._config.batch,
            validate_result=_validate_dense,
        )
        self._sparse_batcher = EmbeddingBatcher[SparseEmbeddingRequest, SparseEmbedding](
            sparse_provider or DeterministicHashSparseEmbeddingProvider(),
            config=self._config.batch,
            validate_result=_validate_sparse,
        )

    @property
    def config(self) -> VectorIngestionConfig:
        """Return the immutable configuration used by this service."""

        return self._config

    async def project(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        document: NormalizedDocument,
        metadata: DocumentMetadata,
        pipeline_version: str,
    ) -> VectorIngestionResult:
        """Return only after every canonical chunk has a vector point."""

        _validate_input(
            tenant_id=tenant_id,
            document_id=document_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            document=document,
            metadata=metadata,
            pipeline_version=pipeline_version,
        )
        try:
            chunks = HierarchicalChunker(self._config.chunking).chunk(document)
            manifest = await self._manifest.persist(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                config=self._config.chunking,
                result=chunks,
            )
        except (ChunkManifestStorageError, ChunkManifestPersistenceError) as exc:
            raise VectorIngestionDependencyError(
                "The canonical chunk manifest dependency failed."
            ) from exc
        except (ChunkingError, ChunkManifestError) as exc:
            raise VectorIngestionInputError(
                "The normalized document cannot produce a canonical chunk manifest."
            ) from exc

        dense_requests = tuple(
            EmbeddingRequest(
                text=chunk.text,
                input_checksum_sha256=chunk.content_checksum_sha256,
                config=self._config.dense,
            )
            for chunk in chunks.chunks
        )
        sparse_requests = tuple(
            SparseEmbeddingRequest(
                text=chunk.text,
                input_checksum_sha256=chunk.content_checksum_sha256,
                config=self._config.sparse,
            )
            for chunk in chunks.chunks
        )
        try:
            dense_results = await self._dense_batcher.embed(dense_requests)
            sparse_results = await self._sparse_batcher.embed(sparse_requests)
        except EmbeddingBatchProviderError as exc:
            raise VectorIngestionDependencyError(
                "An embedding provider failed during bounded projection."
            ) from exc
        except EmbeddingBatchError as exc:
            raise VectorIngestionInputError(
                "An embedding result violated the projection contract."
            ) from exc

        created = 0
        reused = 0
        for chunk, dense, sparse in zip(
            chunks.chunks,
            dense_results,
            sparse_results,
            strict=True,
        ):
            try:
                point = VectorPointRequest(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    chunk=chunk,
                    metadata=metadata,
                    pipeline_version=pipeline_version,
                    collection=self._config.collection,
                    dense=dense,
                    sparse=sparse,
                ).build_point()
                result = await self._vector_index.upsert(point)
            except VectorIndexDependencyError as exc:
                raise VectorIngestionDependencyError(
                    "The vector projection dependency failed."
                ) from exc
            except VectorIndexError as exc:
                raise VectorIngestionInputError(
                    "The vector projection violated its immutable contract."
                ) from exc
            if result.status == "created":
                created += 1
            else:
                reused += 1

        return VectorIngestionResult(
            manifest_artifact_id=manifest.artifact_id,
            manifest_reused=manifest.reused,
            point_count=len(chunks.chunks),
            points_created=created,
            points_reused=reused,
        )


def _validate_input(
    *,
    tenant_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    normalized_artifact_id: UUID,
    document: NormalizedDocument,
    metadata: DocumentMetadata,
    pipeline_version: str,
) -> None:
    if any(
        not isinstance(value, UUID)
        for value in (tenant_id, document_id, document_version_id, normalized_artifact_id)
    ):
        raise VectorIngestionInputError("Vector ingestion requires UUID lineage.")
    if not isinstance(document, NormalizedDocument):
        raise VectorIngestionInputError("Vector ingestion requires a normalized document.")
    if not isinstance(metadata, DocumentMetadata):
        raise VectorIngestionInputError("Vector ingestion requires document metadata.")
    if metadata.source_artifact_checksum != document.checksum_sha256:
        raise VectorIngestionInputError(
            "Vector metadata must belong to the normalized document."
        )
    if not isinstance(pipeline_version, str) or not pipeline_version.strip():
        raise VectorIngestionInputError("Vector ingestion requires a pipeline version.")


def _validate_dense(request: EmbeddingRequest, result: DenseEmbedding) -> DenseEmbedding:
    return validate_embedding_result(request=request, result=result)


def _validate_sparse(
    request: SparseEmbeddingRequest,
    result: SparseEmbedding,
) -> SparseEmbedding:
    return validate_sparse_embedding_result(request=request, result=result)


__all__ = [
    "VectorIngestionConfig",
    "VectorIngestionDependencyError",
    "VectorIngestionError",
    "VectorIngestionInputError",
    "VectorIngestionResult",
    "VectorIngestionService",
]
