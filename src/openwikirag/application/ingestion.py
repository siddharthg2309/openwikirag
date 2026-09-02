"""Recoverable Redis consumer-group orchestration for ingestion jobs."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.infrastructure.models import IngestionJob
from openwikirag.infrastructure.repositories.jobs import (
    JobClaim,
    JobClaimStatus,
    JobRepository,
)
from openwikirag.infrastructure.streams import StreamMessage, StreamTransport
from openwikirag.security.authorization import (
    AuthorizationService,
    Permission,
    Principal,
)


class IngestionHandler(Protocol):
    """Processing port; extraction is intentionally not implemented in this slice."""

    async def handle(self, *, job_id: UUID, payload: dict[str, object]) -> None:
        """Perform the next ingestion step for a claimed job."""


class RetryableJobError(Exception):
    """Raised when an ingestion attempt may succeed on a later delivery."""


class PermanentJobError(Exception):
    """Raised when an ingestion event cannot succeed by retrying."""


class MalformedJobMessageError(Exception):
    """Raised when a stream message cannot be trusted as an ingestion event."""


@dataclass(frozen=True, slots=True)
class IngestionEvent:
    event_id: UUID
    event_type: str
    tenant_id: UUID
    job_id: UUID
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class IngestionJobProgress:
    """Safe, tenant-scoped projection of durable ingestion-job state."""

    job_id: UUID
    document_version_id: UUID
    job_type: str
    status: str
    current_step: str | None
    progress_percent: int
    attempts: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


class JobNotFoundError(Exception):
    """Raised when a job is absent from the caller's tenant scope."""


class IngestionJobProgressService:
    """Read canonical job state without exposing transport-layer details."""

    def __init__(self, session: AsyncSession) -> None:
        self._jobs = JobRepository(session)
        self._authorization = AuthorizationService()

    async def get(
        self,
        *,
        principal: Principal,
        job_id: UUID,
    ) -> IngestionJobProgress | None:
        tenant_id = UUID(principal.tenant_id)
        job = await self._jobs.get_by_id(job_id=job_id, tenant_id=tenant_id)
        if job is None:
            return None

        self._authorization.require(
            principal,
            Permission.READ_DOCUMENTS,
            resource_tenant_id=str(job.tenant_id),
        )
        return _job_progress(job)


def parse_ingestion_event(message: StreamMessage) -> IngestionEvent:
    """Validate the transport envelope before using any ids for database access."""

    required_fields = {"event_id", "event_type", "tenant_id", "aggregate_id", "payload"}
    if not required_fields.issubset(message.fields):
        raise MalformedJobMessageError("The event is missing required fields.")

    try:
        event_id = UUID(message.fields["event_id"])
        tenant_id = UUID(message.fields["tenant_id"])
        job_id = UUID(message.fields["aggregate_id"])
        raw_payload = json.loads(message.fields["payload"])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise MalformedJobMessageError("The event contains invalid identifiers or JSON.") from exc

    if message.fields["event_type"] != "document.ingestion.requested":
        raise MalformedJobMessageError("The event type is not supported.")
    if not isinstance(raw_payload, dict):
        raise MalformedJobMessageError("The event payload must be an object.")
    if raw_payload.get("ingestion_job_id") != str(job_id):
        raise MalformedJobMessageError("The event job id does not match its aggregate id.")

    return IngestionEvent(
        event_id=event_id,
        event_type=message.fields["event_type"],
        tenant_id=tenant_id,
        job_id=job_id,
        payload=raw_payload,
    )


def _job_progress(job: IngestionJob) -> IngestionJobProgress:
    """Map lifecycle state to coarse progress until pipeline steps report detail."""

    available_at = _as_utc(job.available_at)
    assert available_at is not None
    progress_percent = {
        "pending": 0,
        "running": 50,
        "retryable": 0,
        "succeeded": 100,
        "dead_letter": 0,
    }.get(job.status, 0)
    return IngestionJobProgress(
        job_id=job.id,
        document_version_id=job.document_version_id,
        job_type=job.job_type,
        status=job.status,
        current_step=job.current_step,
        progress_percent=progress_percent,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        available_at=available_at,
        started_at=_as_utc(job.started_at),
        completed_at=_as_utc(job.completed_at),
        lease_expires_at=_as_utc(job.lease_expires_at),
        last_error_code=job.last_error_code,
        last_error_message=job.last_error_message,
    )


class IngestionConsumerService:
    """Consume one batch and preserve durable state before acknowledging Redis."""

    def __init__(
        self,
        session: AsyncSession,
        transport: StreamTransport,
        handler: IngestionHandler,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        dead_letter_stream_name: str,
        lease_seconds: int,
        retry_backoff_base_seconds: int,
        retry_backoff_max_seconds: int,
        batch_size: int = 10,
        block_ms: int = 100,
    ) -> None:
        self._session = session
        self._transport = transport
        self._handler = handler
        self._stream_name = stream_name
        self._group_name = group_name
        self._consumer_name = consumer_name
        self._dead_letter_stream_name = dead_letter_stream_name
        self._lease_seconds = lease_seconds
        self._retry_backoff_base_seconds = retry_backoff_base_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._jobs = JobRepository(session)

    async def ensure_group(self) -> None:
        await self._transport.ensure_group(
            stream_name=self._stream_name,
            group_name=self._group_name,
        )

    async def consume_once(self) -> int:
        messages = await self._transport.read(
            stream_name=self._stream_name,
            group_name=self._group_name,
            consumer_name=self._consumer_name,
            count=self._batch_size,
            block_ms=self._block_ms,
        )
        for message in messages:
            await self._handle_message(message)
        return len(messages)

    async def reclaim_once(self) -> int:
        messages = await self._transport.claim_stale(
            stream_name=self._stream_name,
            group_name=self._group_name,
            consumer_name=self._consumer_name,
            min_idle_ms=self._lease_seconds * 1000,
            count=self._batch_size,
        )
        for message in messages:
            await self._handle_message(message)
        return len(messages)

    async def _handle_message(self, message: StreamMessage) -> None:
        try:
            event = parse_ingestion_event(message)
        except MalformedJobMessageError:
            await self._dead_letter_then_ack(message, reason="MALFORMED_EVENT")
            return

        claim = await self._jobs.claim(
            job_id=event.job_id,
            tenant_id=event.tenant_id,
            lease_seconds=self._lease_seconds,
        )
        if claim.status is JobClaimStatus.MISSING:
            await self._session.rollback()
            await self._dead_letter_then_ack(message, reason="JOB_NOT_FOUND")
            return
        if claim.status is JobClaimStatus.TERMINAL:
            await self._session.rollback()
            await self._acknowledge(message)
            return
        if claim.status in {JobClaimStatus.ACTIVE, JobClaimStatus.NOT_DUE}:
            await self._session.rollback()
            return
        if claim.status is JobClaimStatus.EXHAUSTED:
            assert claim.job is not None
            await self._dead_letter_job_then_ack(
                message,
                claim.job,
                reason="MAX_ATTEMPTS_EXCEEDED",
            )
            return

        assert claim.status is JobClaimStatus.CLAIMED
        assert claim.job is not None
        await self._run_claimed_job(message, event, claim)

    async def _run_claimed_job(
        self,
        message: StreamMessage,
        event: IngestionEvent,
        claim: JobClaim,
    ) -> None:
        assert claim.job is not None
        try:
            await self._handler.handle(job_id=event.job_id, payload=event.payload)
        except RetryableJobError:
            await self._jobs.mark_retryable(
                claim.job,
                error_code="INGESTION_RETRYABLE_FAILURE",
                base_backoff_seconds=self._retry_backoff_base_seconds,
                max_backoff_seconds=self._retry_backoff_max_seconds,
            )
            await self._session.commit()
            return
        except PermanentJobError:
            await self._jobs.mark_dead_letter(
                claim.job,
                error_code="INGESTION_PERMANENT_FAILURE",
            )
            await self._dead_letter_then_ack(message, reason="INGESTION_PERMANENT_FAILURE")
            return
        except Exception:
            await self._jobs.mark_retryable(
                claim.job,
                error_code="INGESTION_UNEXPECTED_FAILURE",
                base_backoff_seconds=self._retry_backoff_base_seconds,
                max_backoff_seconds=self._retry_backoff_max_seconds,
            )
            await self._session.commit()
            return

        await self._jobs.mark_succeeded(claim.job)
        await self._session.commit()
        await self._acknowledge(message)

    async def _dead_letter_job_then_ack(
        self,
        message: StreamMessage,
        job: IngestionJob,
        *,
        reason: str,
    ) -> None:
        await self._dead_letter_then_ack(message, reason=reason)
        await self._session.commit()

    async def _dead_letter_then_ack(self, message: StreamMessage, *, reason: str) -> None:
        await self._transport.publish_dead_letter(
            stream_name=self._dead_letter_stream_name,
            message=message,
            reason=reason,
        )
        await self._session.commit()
        await self._acknowledge(message)

    async def _acknowledge(self, message: StreamMessage) -> None:
        await self._transport.acknowledge(
            stream_name=self._stream_name,
            group_name=self._group_name,
            message_id=message.message_id,
        )


def utc_now() -> datetime:
    """Expose one clock seam for callers/tests that need a current timestamp."""

    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite's naive timestamps for a stable API representation."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
