"""Persistence operations for the transactional outbox."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import OutboxEvent


class OutboxRepository:
    """Read and update outbox rows while the caller owns transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_event(
        self,
        *,
        tenant_id: UUID,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, object],
        traceparent: str | None = None,
        occurred_at: datetime | None = None,
    ) -> OutboxEvent:
        event = OutboxEvent(
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload_json=payload,
            traceparent=traceparent,
        )
        if occurred_at is not None:
            event.occurred_at = occurred_at
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_pending(self, *, limit: int) -> list[OutboxEvent]:
        statement = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def mark_published(self, event: OutboxEvent) -> None:
        event.published_at = datetime.now(UTC)
        event.last_error_code = None
        event.last_error_message = None
        await self._session.flush()

    async def record_publish_failure(self, event: OutboxEvent, *, error_code: str) -> None:
        event.publish_attempts += 1
        event.last_error_code = error_code
        event.last_error_message = "The event could not be published to the stream."
        await self._session.flush()
