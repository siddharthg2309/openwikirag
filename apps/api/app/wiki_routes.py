"""HTTP contracts for tenant-scoped WikiRAG pages and review actions."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.wiki import WikiPage
from openwikirag.application.wiki_generation import WikiGenerationResult
from openwikirag.application.wiki_page_artifacts import (
    ReadWikiPageArtifact,
    WikiPageArtifactInputError,
    WikiPageArtifactIntegrityError,
    WikiPageArtifactListResult,
    WikiPageArtifactListService,
    WikiPageArtifactReadService,
    WikiPageArtifactReviewService,
    WikiPageArtifactStorageError,
    WikiPageReviewConflictError,
    WikiPageReviewIntegrityError,
    WikiPageReviewResult,
    WikiPageReviewStatus,
    WikiPageReviewTransitionError,
)
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import AuthorizationError, Principal

from .dependencies import get_current_principal, get_object_storage, get_session

router = APIRouter(prefix="/api/v1/wiki", tags=["wiki"])


class WikiPageArtifactResponse(BaseModel):
    """Validated page package and immutable lineage metadata for a tenant member."""

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
    page: WikiPage
    generation: WikiGenerationResult


class WikiPageReviewRequest(BaseModel):
    """Requested target for the page's mutable review workflow state."""

    status: WikiPageReviewStatus


class WikiPageReviewResponse(BaseModel):
    """Audited result of one page review-status request."""

    artifact_id: UUID
    tenant_id: UUID
    previous_status: str
    review_status: str
    changed: bool


class WikiPageArtifactSummaryResponse(BaseModel):
    """Metadata-only page item returned by the listing endpoint."""

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


class WikiPageArtifactListResponse(BaseModel):
    """One bounded tenant-scoped page metadata window."""

    items: list[WikiPageArtifactSummaryResponse]
    limit: int
    offset: int
    has_more: bool


def _service(
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> WikiPageArtifactReadService:
    return WikiPageArtifactReadService(session, storage)


def _response(page_artifact: ReadWikiPageArtifact) -> WikiPageArtifactResponse:
    return WikiPageArtifactResponse(
        artifact_id=page_artifact.artifact_id,
        tenant_id=page_artifact.tenant_id,
        generation_artifact_id=page_artifact.generation_artifact_id,
        document_version_id=page_artifact.document_version_id,
        normalized_artifact_id=page_artifact.normalized_artifact_id,
        page_checksum=page_artifact.page_checksum,
        generation_result_checksum_sha256=page_artifact.generation_result_checksum_sha256,
        content_checksum_sha256=page_artifact.content_checksum_sha256,
        review_status=page_artifact.review_status,
        created_at=page_artifact.created_at,
        page=page_artifact.package.page,
        generation=page_artifact.package.generation,
    )


def _review_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WikiPageArtifactReviewService:
    return WikiPageArtifactReviewService(session)


def _review_response(result: WikiPageReviewResult) -> WikiPageReviewResponse:
    return WikiPageReviewResponse(
        artifact_id=result.artifact_id,
        tenant_id=result.tenant_id,
        previous_status=result.previous_status,
        review_status=result.review_status,
        changed=result.changed,
    )


def _list_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WikiPageArtifactListService:
    return WikiPageArtifactListService(session)


def _list_response(result: WikiPageArtifactListResult) -> WikiPageArtifactListResponse:
    return WikiPageArtifactListResponse(
        items=[
            WikiPageArtifactSummaryResponse(
                artifact_id=item.artifact_id,
                tenant_id=item.tenant_id,
                generation_artifact_id=item.generation_artifact_id,
                document_version_id=item.document_version_id,
                normalized_artifact_id=item.normalized_artifact_id,
                page_checksum=item.page_checksum,
                generation_result_checksum_sha256=item.generation_result_checksum_sha256,
                content_checksum_sha256=item.content_checksum_sha256,
                review_status=item.review_status,
                created_at=item.created_at,
            )
            for item in result.items
        ],
        limit=result.limit,
        offset=result.offset,
        has_more=result.has_more,
    )


@router.get("/pages", response_model=WikiPageArtifactListResponse)
async def list_wiki_pages(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[WikiPageArtifactListService, Depends(_list_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
    review_status: Annotated[WikiPageReviewStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WikiPageArtifactListResponse:
    """List tenant-owned page metadata without loading object payloads."""

    try:
        result = await service.list(
            principal=principal,
            review_status=review_status,
            limit=limit,
            offset=offset,
        )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity is not authorized to list WikiRAG pages.",
        ) from exc
    except WikiPageArtifactInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The WikiRAG page listing window is invalid.",
        ) from exc
    except WikiPageArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page listing failed integrity validation.",
        ) from exc

    try:
        await AuditRepository(session).record(
            action="wiki.page.list",
            resource_type="wiki_page_artifact",
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            request_id=request.headers.get("X-Request-ID"),
            outcome="success",
            metadata={
                "review_status": (
                    review_status.value if review_status is not None else None
                ),
                "limit": result.limit,
                "offset": result.offset,
                "count": len(result.items),
                "has_more": result.has_more,
            },
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page listing could not be audited.",
        ) from exc

    return _list_response(result)


@router.get("/pages/{artifact_id}", response_model=WikiPageArtifactResponse)
async def get_wiki_page(
    artifact_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[WikiPageArtifactReadService, Depends(_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WikiPageArtifactResponse:
    """Return one integrity-checked page package from the caller's tenant."""

    try:
        page_artifact = await service.get(
            principal=principal,
            artifact_id=artifact_id,
        )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity is not authorized to read WikiRAG pages.",
        ) from exc
    except WikiPageArtifactStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WikiRAG page storage is temporarily unavailable.",
        ) from exc
    except WikiPageArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page artifact failed integrity validation.",
        ) from exc

    if page_artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested WikiRAG page was not found.",
        )

    try:
        await AuditRepository(session).record(
            action="wiki.page.read",
            resource_type="wiki_page_artifact",
            resource_id=str(page_artifact.artifact_id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            request_id=request.headers.get("X-Request-ID"),
            outcome="success",
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page read could not be audited.",
        ) from exc

    return _response(page_artifact)


@router.post("/pages/{artifact_id}/review", response_model=WikiPageReviewResponse)
async def review_wiki_page(
    artifact_id: UUID,
    body: WikiPageReviewRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[WikiPageArtifactReviewService, Depends(_review_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WikiPageReviewResponse:
    """Change review metadata while preserving the immutable page package."""

    try:
        result = await service.transition(
            principal=principal,
            artifact_id=artifact_id,
            target_status=body.status,
        )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity is not authorized to review WikiRAG pages.",
        ) from exc
    except WikiPageReviewTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The requested WikiRAG page review transition is not allowed.",
        ) from exc
    except WikiPageReviewConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The WikiRAG page review status changed; reload and retry.",
        ) from exc
    except WikiPageReviewIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page review status failed integrity validation.",
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested WikiRAG page was not found.",
        )

    try:
        await AuditRepository(session).record(
            action="wiki.page.review",
            resource_type="wiki_page_artifact",
            resource_id=str(result.artifact_id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            request_id=request.headers.get("X-Request-ID"),
            outcome="success",
            metadata={
                "from_status": result.previous_status,
                "to_status": result.review_status,
                "changed": result.changed,
            },
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The WikiRAG page review could not be audited.",
        ) from exc

    return _review_response(result)
