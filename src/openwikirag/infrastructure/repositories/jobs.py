"""Canonical ingestion-job state transitions and lease ownership."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import IngestionJob


class JobClaimStatus(StrEnum):
    CLAIMED = "claimed"
    ACTIVE = "active"
    NOT_DUE = "not_due"
    TERMINAL = "terminal"
    MISSING = "missing"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class JobClaim:
    status: JobClaimStatus
    job: IngestionJob | None


class JobRepository:
    """Apply job state transitions inside a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        job_type: str,
        request_id: str | None = None,
    ) -> IngestionJob:
        """Stage one pending job while the caller owns the transaction."""

        job = IngestionJob(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            job_type=job_type,
            request_id=request_id,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(
        self,
        *,
        job_id: UUID,
        tenant_id: UUID,
    ) -> IngestionJob | None:
        """Load one job only when it belongs to the requested tenant."""

        job = await self._session.scalar(
            select(IngestionJob).where(
                IngestionJob.id == job_id,
                IngestionJob.tenant_id == tenant_id,
            )
        )
        return job

    async def claim(
        self,
        *,
        job_id: UUID,
        tenant_id: UUID,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobClaim:
        current_time = now or datetime.now(UTC)
        job = await self._session.scalar(
            select(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .with_for_update()
        )
        if job is None:
            return JobClaim(JobClaimStatus.MISSING, None)

        if job.status in {"succeeded", "dead_letter"}:
            return JobClaim(JobClaimStatus.TERMINAL, job)

        lease_expires_at = _as_utc(job.lease_expires_at)
        if (
            job.status == "running"
            and lease_expires_at is not None
            and lease_expires_at > current_time
        ):
            return JobClaim(JobClaimStatus.ACTIVE, job)

        available_at = _as_utc(job.available_at)
        if job.status in {"pending", "retryable"} and available_at is not None:
            if available_at > current_time:
                return JobClaim(JobClaimStatus.NOT_DUE, job)

        if job.attempts >= job.max_attempts:
            job.status = "dead_letter"
            job.current_step = "dead_letter"
            job.completed_at = current_time
            job.lease_expires_at = None
            job.last_error_code = "MAX_ATTEMPTS_EXCEEDED"
            await self._session.flush()
            return JobClaim(JobClaimStatus.EXHAUSTED, job)

        job.attempts += 1
        job.status = "running"
        job.current_step = "processing"
        job.started_at = job.started_at or current_time
        job.completed_at = None
        job.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
        await self._session.flush()
        return JobClaim(JobClaimStatus.CLAIMED, job)

    async def mark_succeeded(
        self,
        job: IngestionJob,
        *,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        job.status = "succeeded"
        job.current_step = "complete"
        job.completed_at = current_time
        job.lease_expires_at = None
        job.last_error_code = None
        job.last_error_message = None
        await self._session.flush()

    async def mark_retryable(
        self,
        job: IngestionJob,
        *,
        error_code: str,
        base_backoff_seconds: int,
        max_backoff_seconds: int,
        now: datetime | None = None,
    ) -> int:
        current_time = now or datetime.now(UTC)
        exponent = min(max(int(job.attempts) - 1, 0), 30)
        delay_seconds: int = min(max_backoff_seconds, base_backoff_seconds * (2**exponent))
        job.status = "retryable"
        job.current_step = "retry_wait"
        job.available_at = current_time + timedelta(seconds=delay_seconds)
        job.lease_expires_at = None
        job.last_error_code = error_code
        job.last_error_message = "The ingestion attempt will be retried."
        await self._session.flush()
        return delay_seconds

    async def mark_dead_letter(
        self,
        job: IngestionJob,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        job.status = "dead_letter"
        job.current_step = "dead_letter"
        job.completed_at = current_time
        job.lease_expires_at = None
        job.last_error_code = error_code
        job.last_error_message = "The ingestion job requires operator attention."
        await self._session.flush()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
