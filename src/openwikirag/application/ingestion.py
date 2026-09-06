"""Recoverable Redis consumer-group orchestration for ingestion jobs."""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog
from opentelemetry.trace import SpanKind, Tracer
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.metrics import (
    DEFAULT_METRICS,
    INGESTION_MESSAGE_DURATION_SECONDS,
    INGESTION_MESSAGES_TOTAL,
    MetricsError,
    MetricsRegistry,
)
from openwikirag.core.tracing import (
    extract_trace_context,
    get_tracer,
    mark_span_error,
    safe_span,
    set_span_attribute,
    span_trace_fields,
)
from openwikirag.infrastructure.database import set_tenant_context
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

logger = structlog.get_logger(__name__)


class IngestionHandler(Protocol):
    """Process an already tenant-scoped, lease-claimed ingestion job."""

    async def handle(self, *, job: IngestionJob, payload: dict[str, object]) -> None:
        """Perform the next ingestion step for a claimed job."""


class RetryableJobError(Exception):
    """Raised when an ingestion attempt may succeed on a later delivery."""


class PermanentJobError(Exception):
    """Raised when an ingestion event cannot succeed by retrying."""


class MalformedJobMessageError(Exception):
    """Raised when a stream message cannot be trusted as an ingestion event."""


WIKI_REGENERATION_EVENT_TYPE = "wiki.page.regeneration.requested"
WIKI_REGENERATION_JOB_TYPE = "wiki_regeneration"
_METRIC_OUTCOMES = frozenset(
    {"succeeded", "retryable", "dead_letter", "terminal", "skipped", "error"}
)


INGESTION_EVENT_JOB_TYPES: dict[str, str] = {
    "document.ingestion.requested": "ingestion",
    WIKI_REGENERATION_EVENT_TYPE: WIKI_REGENERATION_JOB_TYPE,
}


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

    if message.fields["event_type"] not in INGESTION_EVENT_JOB_TYPES:
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
        metrics: MetricsRegistry | None = None,
        tracer: Tracer | None = None,
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
        self._metrics = metrics or DEFAULT_METRICS
        self._tracer = tracer or get_tracer()

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
        started = time.perf_counter()
        outcome = "error"
        parent_context = extract_trace_context(message.fields.get("traceparent"))
        event_type = message.fields.get("event_type")
        safe_event_type = event_type if event_type in INGESTION_EVENT_JOB_TYPES else "unknown"
        with safe_span(
            self._tracer,
            "job.consume",
            context=parent_context,
            kind=SpanKind.CONSUMER,
        ) as span:
            set_span_attribute(span, "messaging.system", "redis")
            set_span_attribute(span, "messaging.operation", "process")
            set_span_attribute(span, "event.type", safe_event_type)
            with structlog.contextvars.bound_contextvars(**span_trace_fields(span)):
                try:
                    outcome = await self._handle_message_inner(message)
                except BaseException as exc:
                    mark_span_error(span, exc)
                    raise
                finally:
                    metric_outcome = outcome if outcome in _METRIC_OUTCOMES else "error"
                    set_span_attribute(span, "job.outcome", metric_outcome)
                    try:
                        labels = {"outcome": metric_outcome}
                        self._metrics.increment(INGESTION_MESSAGES_TOTAL, labels=labels)
                        self._metrics.observe(
                            INGESTION_MESSAGE_DURATION_SECONDS,
                            max(time.perf_counter() - started, 0.0),
                            labels=labels,
                        )
                    except MetricsError:
                        logger.warning("ingestion_metrics_record_failed")

    async def _handle_message_inner(self, message: StreamMessage) -> str:
        try:
            event = parse_ingestion_event(message)
        except MalformedJobMessageError:
            await self._dead_letter_then_ack(message, reason="MALFORMED_EVENT")
            return "dead_letter"

        await set_tenant_context(self._session, event.tenant_id)
        claim = await self._jobs.claim(
            job_id=event.job_id,
            tenant_id=event.tenant_id,
            lease_seconds=self._lease_seconds,
        )
        if claim.status is JobClaimStatus.MISSING:
            await self._session.rollback()
            await self._dead_letter_then_ack(message, reason="JOB_NOT_FOUND")
            return "dead_letter"
        if claim.status is JobClaimStatus.TERMINAL:
            await self._session.rollback()
            await self._acknowledge(message)
            return "terminal"
        if claim.status in {JobClaimStatus.ACTIVE, JobClaimStatus.NOT_DUE}:
            await self._session.rollback()
            return "skipped"
        if claim.status is JobClaimStatus.EXHAUSTED:
            assert claim.job is not None
            await self._dead_letter_job_then_ack(
                message,
                claim.job,
                reason="MAX_ATTEMPTS_EXCEEDED",
            )
            return "dead_letter"

        assert claim.status is JobClaimStatus.CLAIMED
        assert claim.job is not None
        expected_job_type = INGESTION_EVENT_JOB_TYPES[event.event_type]
        if claim.job.job_type != expected_job_type:
            await self._jobs.mark_dead_letter(
                claim.job,
                error_code="INGESTION_EVENT_JOB_MISMATCH",
            )
            await self._dead_letter_then_ack(message, reason="INGESTION_EVENT_JOB_MISMATCH")
            return "dead_letter"
        return await self._run_claimed_job(message, event, claim)

    async def _run_claimed_job(
        self,
        message: StreamMessage,
        event: IngestionEvent,
        claim: JobClaim,
    ) -> str:
        assert claim.job is not None
        try:
            await self._handler.handle(job=claim.job, payload=event.payload)
        except RetryableJobError:
            job = await self._refresh_claimed_job(claim.job, tenant_id=event.tenant_id)
            await self._jobs.mark_retryable(
                job,
                error_code="INGESTION_RETRYABLE_FAILURE",
                base_backoff_seconds=self._retry_backoff_base_seconds,
                max_backoff_seconds=self._retry_backoff_max_seconds,
            )
            await self._session.commit()
            return "retryable"
        except PermanentJobError:
            job = await self._refresh_claimed_job(claim.job, tenant_id=event.tenant_id)
            await self._jobs.mark_dead_letter(
                job,
                error_code="INGESTION_PERMANENT_FAILURE",
            )
            await self._dead_letter_then_ack(message, reason="INGESTION_PERMANENT_FAILURE")
            return "dead_letter"
        except Exception:
            job = await self._refresh_claimed_job(claim.job, tenant_id=event.tenant_id)
            await self._jobs.mark_retryable(
                job,
                error_code="INGESTION_UNEXPECTED_FAILURE",
                base_backoff_seconds=self._retry_backoff_base_seconds,
                max_backoff_seconds=self._retry_backoff_max_seconds,
            )
            await self._session.commit()
            return "retryable"

        job = await self._refresh_claimed_job(claim.job, tenant_id=event.tenant_id)
        await self._jobs.mark_succeeded(job)
        await self._session.commit()
        await self._acknowledge(message)
        return "succeeded"

    async def _refresh_claimed_job(self, job: IngestionJob, *, tenant_id: UUID) -> IngestionJob:
        """Reload job state after a handler may have closed its transaction."""

        await set_tenant_context(self._session, tenant_id)
        await self._session.refresh(job)
        return job

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
