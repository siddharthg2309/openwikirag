"""Persist validated WikiRAG generation results as immutable derived artifacts."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.wiki_generation import WikiGenerationResult
from openwikirag.infrastructure.models import WikiGenerationArtifact
from openwikirag.infrastructure.repositories.wiki_generation_artifacts import (
    WikiGenerationArtifactRepository,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError


class WikiGenerationArtifactError(Exception):
    """Base error for generated-artifact persistence failures."""


class WikiGenerationArtifactInputError(WikiGenerationArtifactError):
    """Raised when the caller supplies an invalid generation result."""


class WikiGenerationArtifactSourceNotFoundError(WikiGenerationArtifactError):
    """Raised when a normalized artifact is absent from the tenant/version scope."""


class WikiGenerationArtifactSourceMismatchError(WikiGenerationArtifactError):
    """Raised when the result does not belong to the selected normalized artifact."""


class WikiGenerationArtifactStorageError(WikiGenerationArtifactError):
    """Raised when a generated object cannot be written or verified."""


class WikiGenerationArtifactConflictError(WikiGenerationArtifactError):
    """Raised when immutable metadata or object bytes disagree with a rerun."""


class WikiGenerationArtifactPersistenceError(WikiGenerationArtifactError):
    """Raised when metadata commit fails after an object write."""

    def __init__(self, *, cleanup_failed: bool) -> None:
        super().__init__("The WikiRAG generation artifact metadata could not be recorded.")
        self.cleanup_failed = cleanup_failed


@dataclass(frozen=True, slots=True)
class PersistedWikiGenerationArtifact:
    """Canonical generation artifact identity returned after persistence or reuse."""

    artifact_id: UUID
    document_version_id: UUID
    normalized_artifact_id: UUID
    base_page_checksum: str
    source_artifact_checksum: str
    result_checksum_sha256: str
    artifact_object_key: str
    provider_identity: str
    review_status: str
    reused: bool


class WikiGenerationArtifactService:
    """Write canonical generation JSON before committing its metadata index."""

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._storage = storage
        self._artifacts = WikiGenerationArtifactRepository(session)

    async def persist(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        result: WikiGenerationResult,
    ) -> PersistedWikiGenerationArtifact:
        """Persist one result without crossing tenant, source, or identity boundaries."""

        if not isinstance(result, WikiGenerationResult):
            await self._session.rollback()
            raise WikiGenerationArtifactInputError(
                "Persistence requires a validated WikiGenerationResult."
            )

        source_artifact = await self._artifacts.get_normalized_artifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
        )
        if source_artifact is None:
            await self._session.rollback()
            raise WikiGenerationArtifactSourceNotFoundError(
                "The normalized artifact was not found in the tenant scope."
            )
        if source_artifact.content_checksum_sha256 != result.source_artifact_checksum:
            await self._session.rollback()
            raise WikiGenerationArtifactSourceMismatchError(
                "The generation result belongs to a different normalized artifact."
            )

        result_bytes = result.canonical_bytes()
        result_checksum = result.checksum_sha256
        object_key = _artifact_object_key(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            result=result,
        )
        existing = await self._artifacts.get_by_identity(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            base_page_checksum=result.base_page_checksum,
            generation_version=result.generation_version,
            prompt_checksum=result.prompt_checksum,
            config_hash=result.config_hash,
            provider_identity=result.provider_identity,
        )
        if existing is not None:
            identity_conflicts = (
                existing.result_checksum_sha256 != result_checksum
                or existing.artifact_object_key != object_key
            )
            if identity_conflicts:
                await self._session.rollback()
                raise WikiGenerationArtifactConflictError(
                    "The generation identity already has different immutable content."
                )
            try:
                await self._verify_existing_object(existing)
                existing_result = _result(existing, reused=True)
            except Exception:
                await self._session.rollback()
                raise
            await self._session.rollback()
            return existing_result

        try:
            await self._storage.put(
                object_key=object_key,
                data=result_bytes,
                content_type="application/vnd.openwikirag.wiki-generation+json",
            )
        except ObjectStorageError as exc:
            await self._session.rollback()
            raise WikiGenerationArtifactStorageError(
                "The WikiRAG generation object could not be stored."
            ) from exc

        try:
            artifact = await self._artifacts.create(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                base_page_checksum=result.base_page_checksum,
                source_artifact_checksum=result.source_artifact_checksum,
                metadata_checksum=result.metadata_checksum,
                prompt_checksum=result.prompt_checksum,
                config_hash=result.config_hash,
                provider_identity=result.provider_identity,
                generation_version=result.generation_version,
                result_checksum_sha256=result_checksum,
                artifact_object_key=object_key,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            winner = await self._artifacts.get_by_identity(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                base_page_checksum=result.base_page_checksum,
                generation_version=result.generation_version,
                prompt_checksum=result.prompt_checksum,
                config_hash=result.config_hash,
                provider_identity=result.provider_identity,
            )
            if winner is not None and winner.result_checksum_sha256 == result_checksum:
                if winner.artifact_object_key != object_key:
                    await self._cleanup(object_key)
                    await self._session.rollback()
                    raise WikiGenerationArtifactConflictError(
                        "The concurrent generation winner has a different object key."
                    ) from exc
                try:
                    await self._verify_existing_object(winner)
                    winner_result = _result(winner, reused=True)
                except Exception:
                    await self._session.rollback()
                    raise
                await self._session.rollback()
                return winner_result
            await self._session.rollback()
            await self._cleanup(object_key)
            raise WikiGenerationArtifactConflictError(
                "A concurrent generation conflicts with this immutable artifact."
            ) from exc
        except Exception as exc:
            await self._session.rollback()
            cleanup_failed = await self._cleanup(object_key)
            raise WikiGenerationArtifactPersistenceError(
                cleanup_failed=cleanup_failed
            ) from exc

        return _result(artifact, reused=False)

    async def _verify_existing_object(self, artifact: WikiGenerationArtifact) -> None:
        try:
            data = await self._storage.get(object_key=artifact.artifact_object_key)
        except ObjectStorageError as exc:
            raise WikiGenerationArtifactStorageError(
                "The existing WikiRAG generation object could not be read."
            ) from exc
        if hashlib.sha256(data).hexdigest() != artifact.result_checksum_sha256:
            raise WikiGenerationArtifactConflictError(
                "The existing WikiRAG generation object checksum does not match metadata."
            )

    async def _cleanup(self, object_key: str) -> bool:
        try:
            await self._storage.delete(object_key=object_key)
        except ObjectStorageError:
            return True
        return False


def _artifact_object_key(
    *,
    tenant_id: UUID,
    document_version_id: UUID,
    result: WikiGenerationResult,
) -> str:
    """Return a path containing only validated ids and checksums."""

    return (
        f"tenants/{tenant_id}/document-versions/{document_version_id}/wiki-generation/"
        f"{result.base_page_checksum}/{result.checksum_sha256}.json"
    )


def _result(
    artifact: WikiGenerationArtifact,
    *,
    reused: bool,
) -> PersistedWikiGenerationArtifact:
    return PersistedWikiGenerationArtifact(
        artifact_id=artifact.id,
        document_version_id=artifact.document_version_id,
        normalized_artifact_id=artifact.normalized_artifact_id,
        base_page_checksum=artifact.base_page_checksum,
        source_artifact_checksum=artifact.source_artifact_checksum,
        result_checksum_sha256=artifact.result_checksum_sha256,
        artifact_object_key=artifact.artifact_object_key,
        provider_identity=artifact.provider_identity,
        review_status=artifact.review_status,
        reused=reused,
    )
