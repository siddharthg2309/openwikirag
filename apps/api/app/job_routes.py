"""HTTP contract for tenant-scoped ingestion-job progress."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.ingestion import (
    IngestionJobProgress,
    IngestionJobProgressService,
)
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.security.authorization import AuthorizationError, Principal

from .dependencies import get_current_principal, get_session

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobProgressResponse(BaseModel):
    """Durable lifecycle state safe for a tenant member to poll."""

    job_id: UUID
    document_version_id: UUID
    job_type: str
    status: str
    current_step: str | None
    progress_percent: int = Field(ge=0, le=100)
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    available_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


def _response(progress: IngestionJobProgress) -> JobProgressResponse:
    return JobProgressResponse(
        job_id=progress.job_id,
        document_version_id=progress.document_version_id,
        job_type=progress.job_type,
        status=progress.status,
        current_step=progress.current_step,
        progress_percent=progress.progress_percent,
        attempts=progress.attempts,
        max_attempts=progress.max_attempts,
        available_at=progress.available_at,
        started_at=progress.started_at,
        completed_at=progress.completed_at,
        lease_expires_at=progress.lease_expires_at,
        last_error_code=progress.last_error_code,
        last_error_message=progress.last_error_message,
    )


@router.get("/{job_id}", response_model=JobProgressResponse)
async def get_job_progress(
    job_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JobProgressResponse:
    """Return one job's durable progress for its authenticated tenant."""

    try:
        progress = await IngestionJobProgressService(session).get(
            principal=principal,
            job_id=job_id,
        )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity is not authorized to read job progress.",
        ) from exc

    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested job was not found.",
        )

    await AuditRepository(session).record(
        action="job.read",
        resource_type="ingestion_job",
        resource_id=str(progress.job_id),
        tenant_id=UUID(principal.tenant_id),
        actor_user_id=UUID(principal.subject_id),
        request_id=request.headers.get("X-Request-ID"),
        outcome="success",
    )
    await session.commit()
    return _response(progress)
