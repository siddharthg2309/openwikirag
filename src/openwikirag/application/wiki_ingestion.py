"""Durable worker orchestration for the deterministic WikiRAG pipeline."""

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    ExtractionError,
    ExtractorRegistry,
)
from openwikirag.application.graph_projection import GraphProjection, GraphProjectionError
from openwikirag.application.ingestion import PermanentJobError, RetryableJobError
from openwikirag.application.knowledge import KnowledgeArtifactService, KnowledgeError
from openwikirag.application.metadata import DeterministicMetadataExtractor, MetadataExtractionError
from openwikirag.application.normalized_artifacts import (
    DocumentVersionNotFoundError,
    NormalizedArtifactConflictError,
    NormalizedArtifactPersistenceError,
    NormalizedArtifactService,
    NormalizedArtifactStorageError,
)
from openwikirag.application.ocr import InvalidOcrRequestError, OcrError
from openwikirag.application.vector_index import VectorIndex
from openwikirag.application.vector_ingestion import (
    VectorIngestionConfig,
    VectorIngestionDependencyError,
    VectorIngestionInputError,
    VectorIngestionService,
)
from openwikirag.application.wiki import WikiPageBuilder, WikiPageBuildError
from openwikirag.application.wiki_generation import (
    DeterministicWikiProvider,
    InvalidWikiGenerationOutputError,
    StructuredWikiGenerator,
    WikiGenerationInputError,
    WikiGenerationProvider,
    WikiGenerationProviderError,
)
from openwikirag.application.wiki_generation_artifacts import (
    WikiGenerationArtifactError,
    WikiGenerationArtifactPersistenceError,
    WikiGenerationArtifactService,
    WikiGenerationArtifactStorageError,
)
from openwikirag.application.wiki_page_artifacts import (
    WikiPageArtifactError,
    WikiPageArtifactPersistenceError,
    WikiPageArtifactService,
    WikiPageArtifactStorageError,
)
from openwikirag.infrastructure.models import IngestionJob
from openwikirag.infrastructure.storage import ObjectStorage


class WikiIngestionHandler:
    """Run one claimed job through normalized, WikiRAG, and page artifacts."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        vector_index: VectorIndex,
        *,
        config_hash: str,
        vector_config: VectorIngestionConfig | None = None,
        provider: WikiGenerationProvider | None = None,
        extractors: ExtractorRegistry = DEFAULT_EXTRACTOR_REGISTRY,
        graph: GraphProjection | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._graph = graph
        self._config_hash = config_hash
        self._normalized = NormalizedArtifactService(
            session,
            storage,
            extractors=extractors,
        )
        self._metadata = DeterministicMetadataExtractor()
        self._page_builder = WikiPageBuilder()
        self._generator = StructuredWikiGenerator(
            provider=provider or DeterministicWikiProvider(),
        )
        self._generation_artifacts = WikiGenerationArtifactService(session, storage)
        self._page_artifacts = WikiPageArtifactService(session, storage)
        self._vector_ingestion = VectorIngestionService(
            session,
            storage,
            vector_index,
            config=vector_config,
        )

    async def handle(self, *, job: IngestionJob, payload: dict[str, object]) -> None:
        """Run only the claimed job's canonical version; ignore payload ids."""

        del payload
        job.current_step = "normalize"
        await self._session.flush()
        try:
            normalized = await self._normalized.persist(
                tenant_id=job.tenant_id,
                document_version_id=job.document_version_id,
            )
        except Exception as exc:
            raise _map_normalization_error(exc) from exc

        job.current_step = "metadata"
        await self._session.flush()
        try:
            metadata = self._metadata.extract(
                document=normalized.normalized_document,
            )
            page = self._page_builder.build(
                document=normalized.normalized_document,
                metadata=metadata,
            )
        except (MetadataExtractionError, WikiPageBuildError) as exc:
            await self._session.rollback()
            raise PermanentJobError("The document metadata cannot produce a WikiRAG page.") from exc

        job.current_step = "generate"
        await self._session.flush()
        try:
            generation_result = self._generator.generate(
                document=normalized.normalized_document,
                page=page,
                config_hash=self._config_hash,
            )
        except WikiGenerationProviderError as exc:
            raise RetryableJobError("The WikiRAG generation provider will be retried.") from exc
        except (WikiGenerationInputError, InvalidWikiGenerationOutputError) as exc:
            await self._session.rollback()
            raise PermanentJobError("The WikiRAG generation output is invalid.") from exc

        try:
            generation = await self._generation_artifacts.persist(
                tenant_id=job.tenant_id,
                document_version_id=job.document_version_id,
                normalized_artifact_id=normalized.artifact_id,
                result=generation_result,
            )
        except WikiGenerationArtifactStorageError as exc:
            raise RetryableJobError("The WikiRAG generation artifact will be retried.") from exc
        except WikiGenerationArtifactPersistenceError as exc:
            raise RetryableJobError("The WikiRAG generation metadata will be retried.") from exc
        except WikiGenerationArtifactError as exc:
            await self._session.rollback()
            raise PermanentJobError("The WikiRAG generation artifact is invalid.") from exc

        job.current_step = "page_artifact"
        await self._session.flush()
        try:
            await self._page_artifacts.persist(
                tenant_id=job.tenant_id,
                document_version_id=job.document_version_id,
                normalized_artifact_id=normalized.artifact_id,
                generation_artifact_id=generation.artifact_id,
                page=page,
                generation=generation_result,
            )
        except (
            WikiPageArtifactStorageError,
            WikiPageArtifactPersistenceError,
        ) as exc:
            raise RetryableJobError("The WikiRAG page artifact will be retried.") from exc
        except WikiPageArtifactError as exc:
            await self._session.rollback()
            raise PermanentJobError("The WikiRAG page artifact is invalid.") from exc

        job.current_step = "index_vectors"
        await self._session.flush()
        try:
            projection = await self._vector_ingestion.project(
                tenant_id=job.tenant_id,
                document_id=normalized.document_id,
                document_version_id=job.document_version_id,
                normalized_artifact_id=normalized.artifact_id,
                document=normalized.normalized_document,
                metadata=metadata,
                pipeline_version=normalized.pipeline_version,
            )
        except VectorIngestionDependencyError as exc:
            raise RetryableJobError("The vector projection will be retried.") from exc
        except VectorIngestionInputError as exc:
            await self._session.rollback()
            raise PermanentJobError("The vector projection is invalid.") from exc

        # Reused manifest persistence may rollback and expire ORM attributes.
        await self._session.refresh(job)
        job.current_step = "knowledge_artifact"
        try:
            artifact = await KnowledgeArtifactService(self._session, self._storage).build(
                tenant_id=job.tenant_id, manifest_id=projection.manifest_artifact_id,
            )
            if self._graph is not None:
                await self._graph.upsert(artifact)
        except (KnowledgeError, GraphProjectionError) as exc:
            await self._session.rollback()
            raise RetryableJobError("Canonical graph construction will be retried.") from exc


def _map_normalization_error(error: Exception) -> Exception:
    if isinstance(
        error,
        (OcrError, NormalizedArtifactStorageError, NormalizedArtifactPersistenceError),
    ):
        return RetryableJobError("The normalized artifact will be retried.")
    if isinstance(
        error,
        (
            DocumentVersionNotFoundError,
            NormalizedArtifactConflictError,
            ExtractionError,
            InvalidOcrRequestError,
        ),
    ):
        return PermanentJobError("The document cannot produce a normalized artifact.")
    return RetryableJobError("The normalized artifact will be retried.")
