"""Durable worker orchestration for the deterministic WikiRAG pipeline."""

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    ExtractionError,
    ExtractorRegistry,
)
from openwikirag.application.ingestion import PermanentJobError, RetryableJobError
from openwikirag.application.metadata import DeterministicMetadataExtractor, MetadataExtractionError
from openwikirag.application.normalized_artifacts import (
    DocumentVersionNotFoundError,
    NormalizedArtifactConflictError,
    NormalizedArtifactPersistenceError,
    NormalizedArtifactService,
    NormalizedArtifactStorageError,
)
from openwikirag.application.ocr import InvalidOcrRequestError, OcrError
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
        *,
        config_hash: str,
        provider: WikiGenerationProvider | None = None,
        extractors: ExtractorRegistry = DEFAULT_EXTRACTOR_REGISTRY,
    ) -> None:
        self._session = session
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
