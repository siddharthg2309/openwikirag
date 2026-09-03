"""Persist deterministic normalized-document artifacts outside PostgreSQL."""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    ExtractionError,
    ExtractorRegistry,
    NormalizedDocument,
)
from openwikirag.application.ingestion import PermanentJobError, RetryableJobError
from openwikirag.application.ocr import InvalidOcrRequestError, OcrError
from openwikirag.infrastructure.models import IngestionJob, NormalizedDocumentArtifact
from openwikirag.infrastructure.repositories.normalized_artifacts import (
    NormalizedArtifactRepository,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError


class NormalizedArtifactError(Exception):
    """Base error for normalized-artifact orchestration failures."""


class DocumentVersionNotFoundError(NormalizedArtifactError):
    """Raised when a source version is outside the caller's tenant scope."""


class NormalizedArtifactStorageError(NormalizedArtifactError):
    """Raised when the source or normalized artifact object cannot be accessed."""


class NormalizedArtifactConflictError(NormalizedArtifactError):
    """Raised when one parser identity would overwrite immutable output."""


class NormalizedArtifactPersistenceError(NormalizedArtifactError):
    """Raised when metadata cannot be committed after writing the artifact object."""

    def __init__(self, *, cleanup_failed: bool) -> None:
        super().__init__("The normalized artifact metadata could not be recorded.")
        self.cleanup_failed = cleanup_failed


class NormalizedArtifactIngestionHandler:
    """Map claimed ingestion jobs to durable normalized-artifact persistence."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        extractors: ExtractorRegistry = DEFAULT_EXTRACTOR_REGISTRY,
    ) -> None:
        self._artifacts = NormalizedArtifactService(session, storage, extractors=extractors)

    async def handle(self, *, job: IngestionJob, payload: dict[str, object]) -> None:
        """Persist the job's canonical source version, never a payload-selected one."""

        del payload
        try:
            await self._artifacts.persist(
                tenant_id=job.tenant_id,
                document_version_id=job.document_version_id,
            )
        except (
            DocumentVersionNotFoundError,
            NormalizedArtifactConflictError,
            ExtractionError,
            InvalidOcrRequestError,
        ) as exc:
            raise PermanentJobError("The document cannot produce a normalized artifact.") from exc
        except OcrError as exc:
            raise RetryableJobError("The OCR provider will be retried.") from exc
        except (NormalizedArtifactStorageError, NormalizedArtifactPersistenceError) as exc:
            raise RetryableJobError("The normalized artifact will be retried.") from exc


@dataclass(frozen=True, slots=True)
class PersistedNormalizedArtifact:
    """Canonical artifact identity returned after persistence or safe reuse."""

    artifact_id: UUID
    document_version_id: UUID
    parser_name: str
    parser_version: str
    checksum_sha256: str
    artifact_object_key: str
    character_count: int
    span_count: int
    normalized_document: NormalizedDocument
    reused: bool


class NormalizedArtifactService:
    """Read raw sources, extract deterministically, and persist immutable output."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        extractors: ExtractorRegistry = DEFAULT_EXTRACTOR_REGISTRY,
    ) -> None:
        self._session = session
        self._storage = storage
        self._extractors = extractors
        self._artifacts = NormalizedArtifactRepository(session)

    async def persist(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
    ) -> PersistedNormalizedArtifact:
        version = await self._artifacts.get_document_version(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
        )
        if version is None:
            await self._session.rollback()
            raise DocumentVersionNotFoundError("The document version was not found.")

        try:
            source_data = await self._storage.get(object_key=version.source_object_key)
        except ObjectStorageError as exc:
            await self._session.rollback()
            raise NormalizedArtifactStorageError("The source object could not be read.") from exc

        try:
            normalized = self._extractors.extract(
                source_type=version.source_type,
                data=source_data,
            )
        except ExtractionError:
            await self._session.rollback()
            raise
        artifact_bytes = normalized.canonical_bytes()
        checksum = normalized.checksum_sha256
        object_key = _artifact_object_key(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized=normalized,
        )

        existing = await self._artifacts.get_by_parser_identity(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            parser_name=normalized.parser_name,
            parser_version=normalized.parser_version,
        )
        if existing is not None:
            matches_existing = (
                existing.content_checksum_sha256 == checksum
                and existing.artifact_object_key == object_key
            )
            existing_result = _result(
                existing,
                normalized_document=normalized,
                reused=True,
            )
            await self._session.rollback()
            if matches_existing:
                return existing_result
            raise NormalizedArtifactConflictError(
                "This parser identity already has a different immutable artifact."
            )

        try:
            await self._storage.put(
                object_key=object_key,
                data=artifact_bytes,
                content_type="application/vnd.openwikirag.normalized-document+json",
            )
        except ObjectStorageError as exc:
            await self._session.rollback()
            raise NormalizedArtifactStorageError(
                "The normalized artifact object could not be stored."
            ) from exc

        try:
            artifact = await self._artifacts.create(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                parser_name=normalized.parser_name,
                parser_version=normalized.parser_version,
                content_checksum_sha256=checksum,
                artifact_object_key=object_key,
                character_count=len(normalized.text),
                span_count=len(normalized.spans),
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            winner = await self._artifacts.get_by_parser_identity(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                parser_name=normalized.parser_name,
                parser_version=normalized.parser_version,
            )
            if (
                winner is not None
                and winner.content_checksum_sha256 == checksum
                and winner.artifact_object_key == object_key
            ):
                winner_result = _result(
                    winner,
                    normalized_document=normalized,
                    reused=True,
                )
                await self._session.rollback()
                return winner_result
            await self._cleanup(object_key)
            raise NormalizedArtifactConflictError(
                "A concurrent parser result conflicts with this immutable artifact."
            ) from exc
        except Exception as exc:
            await self._session.rollback()
            cleanup_failed = await self._cleanup(object_key)
            raise NormalizedArtifactPersistenceError(cleanup_failed=cleanup_failed) from exc

        return _result(
            artifact,
            normalized_document=normalized,
            reused=False,
        )

    async def _cleanup(self, object_key: str) -> bool:
        try:
            await self._storage.delete(object_key=object_key)
        except ObjectStorageError:
            return True
        return False


_SAFE_KEY_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _artifact_object_key(
    *,
    tenant_id: UUID,
    document_version_id: UUID,
    normalized: NormalizedDocument,
) -> str:
    return (
        f"tenants/{tenant_id}/document-versions/{document_version_id}/normalized/"
        f"{_safe_key_component(normalized.parser_name)}/"
        f"{_safe_key_component(normalized.parser_version)}/"
        f"{normalized.checksum_sha256}.json"
    )


def _safe_key_component(value: str) -> str:
    normalized = _SAFE_KEY_COMPONENT.sub("-", value).strip(".-")
    return normalized or "unknown"


def _result(
    artifact: NormalizedDocumentArtifact,
    *,
    normalized_document: NormalizedDocument,
    reused: bool,
) -> PersistedNormalizedArtifact:
    return PersistedNormalizedArtifact(
        artifact_id=artifact.id,
        document_version_id=artifact.document_version_id,
        parser_name=artifact.parser_name,
        parser_version=artifact.parser_version,
        checksum_sha256=artifact.content_checksum_sha256,
        artifact_object_key=artifact.artifact_object_key,
        character_count=artifact.character_count,
        span_count=artifact.span_count,
        normalized_document=normalized_document,
        reused=reused,
    )
