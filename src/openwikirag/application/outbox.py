"""Publish committed outbox rows to the asynchronous ingestion stream."""

from dataclasses import dataclass
from uuid import UUID

from opentelemetry.trace import Span, SpanKind, Tracer
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.tracing import (
    extract_trace_context,
    get_tracer,
    inject_traceparent,
    mark_span_error,
    safe_span,
    set_span_attribute,
)
from openwikirag.infrastructure.models import OutboxEvent
from openwikirag.infrastructure.repositories.outbox import OutboxRepository
from openwikirag.infrastructure.streams import StreamPublisher


@dataclass(frozen=True, slots=True)
class PublishedOutboxEvent:
    event_id: UUID
    stream_message_id: str


class OutboxPublisherService:
    """Relay committed events with publish-before-mark ordering."""

    _publish_failure_code = "REDIS_STREAM_PUBLISH_FAILED"

    def __init__(
        self,
        session: AsyncSession,
        publisher: StreamPublisher,
        *,
        stream_name: str,
        tracer: Tracer | None = None,
    ) -> None:
        self._session = session
        self._outbox = OutboxRepository(session)
        self._publisher = publisher
        self._stream_name = stream_name
        self._tracer = tracer if tracer is not None else get_tracer()

    async def publish_pending(self, *, limit: int) -> list[PublishedOutboxEvent]:
        if limit < 1:
            raise ValueError("The outbox batch limit must be positive.")

        pending = await self._outbox.list_pending(limit=limit)
        event_ids = [event.id for event in pending]
        await self._session.rollback()

        published: list[PublishedOutboxEvent] = []
        for event_id in event_ids:
            event = await self._session.get(OutboxEvent, event_id)
            if event is None or event.published_at is not None:
                continue

            span: Span | None = None
            try:
                parent_context = extract_trace_context(event.traceparent)
                with safe_span(
                    self._tracer,
                    "job.publish",
                    context=parent_context,
                    kind=SpanKind.PRODUCER,
                ) as span:
                    try:
                        set_span_attribute(span, "messaging.system", "redis")
                        set_span_attribute(span, "messaging.operation", "publish")
                        set_span_attribute(span, "messaging.destination", self._stream_name)
                        set_span_attribute(span, "event.type", event.event_type)
                        stream_message_id = await self._publisher.publish(
                            stream_name=self._stream_name,
                            event_id=str(event.id),
                            event_type=event.event_type,
                            tenant_id=str(event.tenant_id),
                            aggregate_id=event.aggregate_id,
                            payload=event.payload_json,
                            traceparent=inject_traceparent(),
                        )
                        await self._outbox.mark_published(event)
                        await self._session.commit()
                    except Exception as exc:
                        mark_span_error(span, exc)
                        raise
            except Exception as exc:
                if span is not None:
                    mark_span_error(span, exc)
                await self._session.rollback()
                await self._record_failure(event_id)
                continue

            published.append(
                PublishedOutboxEvent(
                    event_id=event.id,
                    stream_message_id=stream_message_id,
                )
            )

        return published

    async def _record_failure(self, event_id: UUID) -> None:
        try:
            event = await self._session.get(OutboxEvent, event_id)
            if event is not None and event.published_at is None:
                await self._outbox.record_publish_failure(
                    event,
                    error_code=self._publish_failure_code,
                )
                await self._session.commit()
        except Exception:
            await self._session.rollback()
