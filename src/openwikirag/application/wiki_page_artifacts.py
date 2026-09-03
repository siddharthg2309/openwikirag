"""Package deterministic WikiRAG pages with validated generated content."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.wiki import WikiPage
from openwikirag.application.wiki_generation import WikiGenerationResult
from openwikirag.infrastructure.models import WikiGenerationArtifact, WikiPageArtifact
from openwikirag.infrastructure.repositories.wiki_page_artifacts import (
    WikiPageArtifactRepository,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError
from openwikirag.security.authorization import AuthorizationService, Permission, Principal


class WikiPageArtifactError(Exception):
    """Base error for self-contained page-artifact persistence failures."""


class WikiPageArtifactInputError(WikiPageArtifactError):
    """Raised when a page and generation result cannot form one package."""


class WikiPageArtifactSourceNotFoundError(WikiPageArtifactError):
    """Raised when the generation artifact is absent from the tenant scope."""


class WikiPageArtifactSourceMismatchError(WikiPageArtifactError):
    """Raised when generation metadata disagrees with the supplied page/result."""


class WikiPageArtifactStorageError(WikiPageArtifactError):
    """Raised when a page-artifact object cannot be written, read, or verified."""


class WikiPageArtifactConflictError(WikiPageArtifactError):
    """Raised when immutable page-artifact metadata or bytes disagree."""


class WikiPageArtifactIntegrityError(WikiPageArtifactError):
    """Raised when stored page-artifact bytes fail checksum or schema validation."""


class WikiPageArtifactPersistenceError(WikiPageArtifactError):
    """Raised when metadata commit fails after an object write."""

    def __init__(self, *, cleanup_failed: bool) -> None:
        super().__init__("The WikiRAG page-artifact metadata could not be recorded.")
        self.cleanup_failed = cleanup_failed


class WikiPageReviewError(WikiPageArtifactError):
    """Base error for review-state transition failures."""


class WikiPageReviewTransitionError(WikiPageReviewError):
    """Raised when a requested review status transition is not allowed."""


class WikiPageReviewConflictError(WikiPageReviewError):
    """Raised when another writer changed the status before this update."""


class WikiPageReviewIntegrityError(WikiPageReviewError):
    """Raised when persisted review metadata contains an unknown status."""


WIKI_PAGE_ARTIFACT_SCHEMA_VERSION: Literal["wiki-generated-page-v1"] = "wiki-generated-page-v1"


class WikiPageReviewStatus(StrEnum):
    """Review states supported by the page-artifact workflow."""

    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"


_ALLOWED_REVIEW_TRANSITIONS: dict[
    WikiPageReviewStatus, frozenset[WikiPageReviewStatus]
] = {
    WikiPageReviewStatus.DRAFT: frozenset({WikiPageReviewStatus.NEEDS_REVIEW}),
    WikiPageReviewStatus.NEEDS_REVIEW: frozenset(
        {WikiPageReviewStatus.DRAFT, WikiPageReviewStatus.APPROVED}
    ),
    WikiPageReviewStatus.APPROVED: frozenset({WikiPageReviewStatus.NEEDS_REVIEW}),
}


class WikiGeneratedPage(BaseModel):
    """A complete page package with deterministic and model-owned layers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["wiki-generated-page-v1"] = WIKI_PAGE_ARTIFACT_SCHEMA_VERSION
    page: WikiPage
    generation: WikiGenerationResult

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        """Reject a package whose two immutable layers have different lineage."""

        if self.page.checksum_sha256 != self.generation.base_page_checksum:
            raise ValueError("The generated page does not match the base page checksum.")
        if self.page.source_artifact_checksum != self.generation.source_artifact_checksum:
            raise ValueError("The generated page does not match the source checksum.")
        if self.page.metadata_checksum != self.generation.metadata_checksum:
            raise ValueError("The generated page does not match the metadata checksum.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible composite page representation."""

        return self.model_dump(mode="json", exclude_none=True)

    def canonical_bytes(self) -> bytes:
        """Serialize the complete page package deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the immutable identity of the composite page package."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PersistedWikiPageArtifact:
    """Canonical page-artifact identity returned after persistence or reuse."""

    artifact_id: UUID
    tenant_id: UUID
    generation_artifact_id: UUID
    document_version_id: UUID
    normalized_artifact_id: UUID
    page_checksum: str
    generation_result_checksum_sha256: str
    content_checksum_sha256: str
    artifact_object_key: str
    review_status: str
    reused: bool


@dataclass(frozen=True, slots=True)
class ReadWikiPageArtifact:
    """Validated page package returned by the tenant-scoped read service."""

    artifact_id: UUID
    tenant_id: UUID
    generation_artifact_id: UUID
    document_version_id: UUID
    normalized_artifact_id: UUID
    page_checksum: str
    generation_result_checksum_sha256: str
    content_checksum_sha256: str
    review_status: str
    created_at: datetime
    package: WikiGeneratedPage


@dataclass(frozen=True, slots=True)
class WikiPageReviewResult:
    """Outcome of one authorized review-status request."""

    artifact_id: UUID
    tenant_id: UUID
    previous_status: str
    review_status: str
    changed: bool


@dataclass(frozen=True, slots=True)
class WikiPageArtifactSummary:
    """Metadata-only page artifact representation for list responses."""

    artifact_id: UUID
    tenant_id: UUID
    generation_artifact_id: UUID
    document_version_id: UUID
    normalized_artifact_id: UUID
    page_checksum: str
    generation_result_checksum_sha256: str
    content_checksum_sha256: str
    review_status: WikiPageReviewStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WikiPageArtifactListResult:
    """One bounded page-artifact listing window."""

    items: tuple[WikiPageArtifactSummary, ...]
    limit: int
    offset: int
    has_more: bool


class WikiPageArtifactService:
    """Persist a complete page package without mutating its source layers."""

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._storage = storage
        self._artifacts = WikiPageArtifactRepository(session)

    async def persist(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        generation_artifact_id: UUID,
        page: WikiPage,
        generation: WikiGenerationResult,
    ) -> PersistedWikiPageArtifact:
        """Persist one self-contained page package inside one tenant lineage."""

        if not isinstance(page, WikiPage) or not isinstance(generation, WikiGenerationResult):
            await self._session.rollback()
            raise WikiPageArtifactInputError(
                "Persistence requires a WikiPage and a validated WikiGenerationResult."
            )
        try:
            package = WikiGeneratedPage(page=page, generation=generation)
        except (TypeError, ValidationError, ValueError) as exc:
            await self._session.rollback()
            raise WikiPageArtifactInputError(
                "The WikiPage and generation result have inconsistent lineage."
            ) from exc

        generation_artifact = await self._artifacts.get_generation_artifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            generation_artifact_id=generation_artifact_id,
        )
        if generation_artifact is None:
            await self._session.rollback()
            raise WikiPageArtifactSourceNotFoundError(
                "The generation artifact was not found in the tenant scope."
            )
        if not _generation_lineage_matches(generation_artifact, page, generation):
            await self._session.rollback()
            raise WikiPageArtifactSourceMismatchError(
                "The page package does not match the persisted generation artifact."
            )

        content_bytes = package.canonical_bytes()
        content_checksum = package.checksum_sha256
        object_key = _artifact_object_key(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            page=page,
            content_checksum=content_checksum,
        )
        existing = await self._artifacts.get_by_identity(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            generation_artifact_id=generation_artifact_id,
            page_checksum=page.checksum_sha256,
        )
        if existing is not None:
            if _artifact_identity_conflicts(
                existing=existing,
                generation=generation,
                content_checksum=content_checksum,
                object_key=object_key,
            ):
                await self._session.rollback()
                raise WikiPageArtifactConflictError(
                    "The page-artifact identity already has different immutable content."
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
                data=content_bytes,
                content_type="application/vnd.openwikirag.wiki-page+json",
            )
        except ObjectStorageError as exc:
            await self._session.rollback()
            raise WikiPageArtifactStorageError(
                "The WikiRAG page-artifact object could not be stored."
            ) from exc

        try:
            artifact = await self._artifacts.create(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                generation_artifact_id=generation_artifact_id,
                page_checksum=page.checksum_sha256,
                generation_result_checksum_sha256=generation.checksum_sha256,
                content_checksum_sha256=content_checksum,
                artifact_object_key=object_key,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            winner = await self._artifacts.get_by_identity(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                generation_artifact_id=generation_artifact_id,
                page_checksum=page.checksum_sha256,
            )
            if winner is not None and not _artifact_identity_conflicts(
                existing=winner,
                generation=generation,
                content_checksum=content_checksum,
                object_key=object_key,
            ):
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
            raise WikiPageArtifactConflictError(
                "A concurrent page-artifact write conflicts with this immutable package."
            ) from exc
        except Exception as exc:
            await self._session.rollback()
            cleanup_failed = await self._cleanup(object_key)
            raise WikiPageArtifactPersistenceError(
                cleanup_failed=cleanup_failed
            ) from exc

        return _result(artifact, reused=False)

    async def _verify_existing_object(self, artifact: WikiPageArtifact) -> None:
        try:
            data = await self._storage.get(object_key=artifact.artifact_object_key)
        except ObjectStorageError as exc:
            raise WikiPageArtifactStorageError(
                "The existing WikiRAG page-artifact object could not be read."
            ) from exc
        if hashlib.sha256(data).hexdigest() != artifact.content_checksum_sha256:
            raise WikiPageArtifactConflictError(
                "The existing WikiRAG page-artifact object checksum does not match metadata."
            )

    async def _cleanup(self, object_key: str) -> bool:
        try:
            await self._storage.delete(object_key=object_key)
        except ObjectStorageError:
            return True
        return False


class WikiPageArtifactReadService:
    """Read and validate one page package for the authenticated tenant."""

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._storage = storage
        self._artifacts = WikiPageArtifactRepository(session)
        self._authorization = AuthorizationService()

    async def get(
        self,
        *,
        principal: Principal,
        artifact_id: UUID,
    ) -> ReadWikiPageArtifact | None:
        """Return a page only after authorization, checksum, and schema checks."""

        self._authorization.require(principal, Permission.READ_DOCUMENTS)
        artifact = await self._artifacts.get_by_id(
            tenant_id=UUID(principal.tenant_id),
            artifact_id=artifact_id,
        )
        if artifact is None:
            return None

        try:
            data = await self._storage.get(object_key=artifact.artifact_object_key)
        except ObjectStorageError as exc:
            raise WikiPageArtifactStorageError(
                "The WikiRAG page-artifact object could not be read."
            ) from exc
        if hashlib.sha256(data).hexdigest() != artifact.content_checksum_sha256:
            raise WikiPageArtifactIntegrityError(
                "The page-artifact object checksum does not match metadata."
            )

        try:
            package = WikiGeneratedPage.model_validate(json.loads(data))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise WikiPageArtifactIntegrityError(
                "The page-artifact object does not match its schema."
            ) from exc
        if (
            package.checksum_sha256 != artifact.content_checksum_sha256
            or package.page.checksum_sha256 != artifact.page_checksum
            or package.generation.checksum_sha256
            != artifact.generation_result_checksum_sha256
        ):
            raise WikiPageArtifactIntegrityError(
                "The page-artifact object does not match metadata lineage."
            )

        return ReadWikiPageArtifact(
            artifact_id=artifact.id,
            tenant_id=artifact.tenant_id,
            generation_artifact_id=artifact.generation_artifact_id,
            document_version_id=artifact.document_version_id,
            normalized_artifact_id=artifact.normalized_artifact_id,
            page_checksum=artifact.page_checksum,
            generation_result_checksum_sha256=artifact.generation_result_checksum_sha256,
            content_checksum_sha256=artifact.content_checksum_sha256,
            review_status=artifact.review_status,
            created_at=artifact.created_at,
            package=package,
        )


class WikiPageArtifactReviewService:
    """Transition mutable review metadata without changing immutable page bytes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._artifacts = WikiPageArtifactRepository(session)
        self._authorization = AuthorizationService()

    async def transition(
        self,
        *,
        principal: Principal,
        artifact_id: UUID,
        target_status: WikiPageReviewStatus,
    ) -> WikiPageReviewResult | None:
        """Apply one tenant-scoped, compare-and-set review transition."""

        self._authorization.require(principal, Permission.REVIEW_WIKI_PAGES)
        tenant_id = UUID(principal.tenant_id)
        artifact = await self._artifacts.get_by_id(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
        )
        if artifact is None:
            return None

        try:
            current_status = WikiPageReviewStatus(artifact.review_status)
            target_status = WikiPageReviewStatus(target_status)
        except (TypeError, ValueError) as exc:
            await self._session.rollback()
            raise WikiPageReviewIntegrityError(
                "The page artifact contains an unsupported review status."
            ) from exc

        if target_status is current_status:
            return WikiPageReviewResult(
                artifact_id=artifact.id,
                tenant_id=artifact.tenant_id,
                previous_status=current_status.value,
                review_status=current_status.value,
                changed=False,
            )
        if target_status not in _ALLOWED_REVIEW_TRANSITIONS[current_status]:
            await self._session.rollback()
            raise WikiPageReviewTransitionError(
                f"Cannot transition a page from '{current_status.value}' "
                f"to '{target_status.value}'."
            )

        updated = await self._artifacts.compare_and_set_review_status(
            tenant_id=tenant_id,
            artifact_id=artifact.id,
            expected_status=current_status.value,
            new_status=target_status.value,
        )
        if not updated:
            await self._session.rollback()
            raise WikiPageReviewConflictError(
                "The page review status changed before this request committed."
            )
        return WikiPageReviewResult(
            artifact_id=artifact.id,
            tenant_id=artifact.tenant_id,
            previous_status=current_status.value,
            review_status=target_status.value,
            changed=True,
        )


class WikiPageArtifactListService:
    """List tenant-owned page metadata without reading object storage."""

    MAX_LIMIT = 100

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._artifacts = WikiPageArtifactRepository(session)
        self._authorization = AuthorizationService()

    async def list(
        self,
        *,
        principal: Principal,
        review_status: WikiPageReviewStatus | None,
        limit: int,
        offset: int,
    ) -> WikiPageArtifactListResult:
        """Return one tenant-filtered metadata window and a continuation flag."""

        self._authorization.require(principal, Permission.READ_DOCUMENTS)
        if limit < 1 or limit > self.MAX_LIMIT or offset < 0:
            await self._session.rollback()
            raise WikiPageArtifactInputError("The page listing window is invalid.")

        rows = await self._artifacts.list_page_artifacts(
            tenant_id=UUID(principal.tenant_id),
            review_status=review_status.value if review_status is not None else None,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        summaries: list[WikiPageArtifactSummary] = []
        for artifact in rows[:limit]:
            try:
                status_value = WikiPageReviewStatus(artifact.review_status)
            except (TypeError, ValueError) as exc:
                await self._session.rollback()
                raise WikiPageArtifactIntegrityError(
                    "The page artifact contains an unsupported review status."
                ) from exc
            summaries.append(
                WikiPageArtifactSummary(
                    artifact_id=artifact.id,
                    tenant_id=artifact.tenant_id,
                    generation_artifact_id=artifact.generation_artifact_id,
                    document_version_id=artifact.document_version_id,
                    normalized_artifact_id=artifact.normalized_artifact_id,
                    page_checksum=artifact.page_checksum,
                    generation_result_checksum_sha256=(
                        artifact.generation_result_checksum_sha256
                    ),
                    content_checksum_sha256=artifact.content_checksum_sha256,
                    review_status=status_value,
                    created_at=artifact.created_at,
                )
            )
        return WikiPageArtifactListResult(
            items=tuple(summaries),
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

def _generation_lineage_matches(
    generation_artifact: WikiGenerationArtifact,
    page: WikiPage,
    generation: WikiGenerationResult,
) -> bool:
    return (
        generation_artifact.result_checksum_sha256 == generation.checksum_sha256
        and generation_artifact.base_page_checksum == page.checksum_sha256
        and generation_artifact.source_artifact_checksum == generation.source_artifact_checksum
        and generation_artifact.metadata_checksum == generation.metadata_checksum
        and generation_artifact.generation_version == generation.generation_version
        and generation_artifact.prompt_checksum == generation.prompt_checksum
        and generation_artifact.config_hash == generation.config_hash
        and generation_artifact.provider_identity == generation.provider_identity
    )


def _artifact_identity_conflicts(
    *,
    existing: WikiPageArtifact,
    generation: WikiGenerationResult,
    content_checksum: str,
    object_key: str,
) -> bool:
    return (
        existing.generation_result_checksum_sha256 != generation.checksum_sha256
        or existing.content_checksum_sha256 != content_checksum
        or existing.artifact_object_key != object_key
    )


def _artifact_object_key(
    *,
    tenant_id: UUID,
    document_version_id: UUID,
    page: WikiPage,
    content_checksum: str,
) -> str:
    """Return a path containing only validated ids and checksums."""

    return (
        f"tenants/{tenant_id}/document-versions/{document_version_id}/wiki-pages/"
        f"{page.checksum_sha256}/{content_checksum}.json"
    )


def _result(
    artifact: WikiPageArtifact,
    *,
    reused: bool,
) -> PersistedWikiPageArtifact:
    return PersistedWikiPageArtifact(
        artifact_id=artifact.id,
        tenant_id=artifact.tenant_id,
        generation_artifact_id=artifact.generation_artifact_id,
        document_version_id=artifact.document_version_id,
        normalized_artifact_id=artifact.normalized_artifact_id,
        page_checksum=artifact.page_checksum,
        generation_result_checksum_sha256=artifact.generation_result_checksum_sha256,
        content_checksum_sha256=artifact.content_checksum_sha256,
        artifact_object_key=artifact.artifact_object_key,
        review_status=artifact.review_status,
        reused=reused,
    )
