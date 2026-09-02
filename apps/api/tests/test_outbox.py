from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.outbox import OutboxPublisherService
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import Base, OutboxEvent, Tenant
from openwikirag.infrastructure.repositories.outbox import OutboxRepository


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    async with session_factory() as test_session:
        yield test_session
    await engine.dispose()


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def publish(
        self,
        *,
        stream_name: str,
        event_id: str,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        payload: dict[str, object],
    ) -> str:
        message = {
            "stream_name": stream_name,
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "aggregate_id": aggregate_id,
            "payload": payload,
        }
        self.messages.append(message)
        return f"{len(self.messages)}-0"


class FailingPublisher:
    async def publish(
        self,
        *,
        stream_name: str,
        event_id: str,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        payload: dict[str, object],
    ) -> str:
        raise ConnectionError("Redis is unavailable")


async def create_pending_event(session: AsyncSession) -> OutboxEvent:
    tenant = Tenant(name="Outbox Tenant")
    session.add(tenant)
    await session.flush()
    event = await OutboxRepository(session).create_event(
        tenant_id=tenant.id,
        aggregate_type="ingestion_job",
        aggregate_id=str(uuid4()),
        event_type="document.ingestion.requested",
        payload={"document_id": str(uuid4()), "document_version_id": str(uuid4())},
    )
    await session.commit()
    return event


async def test_publisher_marks_event_only_after_stream_ack(session: AsyncSession) -> None:
    event = await create_pending_event(session)
    publisher = RecordingPublisher()
    service = OutboxPublisherService(
        session,
        publisher,
        stream_name="openwikirag:test-ingestion",
    )

    published = await service.publish_pending(limit=10)

    assert len(published) == 1
    assert published[0].event_id == event.id
    assert publisher.messages[0]["event_id"] == str(event.id)
    assert publisher.messages[0]["stream_name"] == "openwikirag:test-ingestion"
    refreshed = await session.get(OutboxEvent, event.id)
    assert refreshed is not None
    assert refreshed.published_at is not None
    assert refreshed.publish_attempts == 0


async def test_redis_failure_leaves_event_pending_and_records_attempt(
    session: AsyncSession,
) -> None:
    event = await create_pending_event(session)
    service = OutboxPublisherService(
        session,
        FailingPublisher(),
        stream_name="openwikirag:test-ingestion",
    )

    published = await service.publish_pending(limit=10)

    assert published == []
    refreshed = await session.get(OutboxEvent, event.id)
    assert refreshed is not None
    assert refreshed.published_at is None
    assert refreshed.publish_attempts == 1
    assert refreshed.last_error_code == "REDIS_STREAM_PUBLISH_FAILED"


async def test_repeated_publishing_reuses_stable_event_id_after_commit_interruption(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await create_pending_event(session)
    publisher = RecordingPublisher()
    original_commit = session.commit
    fail_next_commit = True

    async def flaky_commit() -> None:
        nonlocal fail_next_commit
        if fail_next_commit:
            fail_next_commit = False
            raise ConnectionError("database commit interrupted")
        await original_commit()

    monkeypatch.setattr(session, "commit", flaky_commit)
    service = OutboxPublisherService(
        session,
        publisher,
        stream_name="openwikirag:test-ingestion",
    )

    first_attempt = await service.publish_pending(limit=10)
    second_attempt = await service.publish_pending(limit=10)

    assert first_attempt == []
    assert len(second_attempt) == 1
    assert [message["event_id"] for message in publisher.messages] == [
        str(event.id),
        str(event.id),
    ]
    refreshed = await session.get(OutboxEvent, event.id)
    assert refreshed is not None
    assert refreshed.published_at is not None


async def test_publisher_requires_positive_batch_limit(session: AsyncSession) -> None:
    service = OutboxPublisherService(
        session,
        RecordingPublisher(),
        stream_name="openwikirag:test-ingestion",
    )

    with pytest.raises(ValueError):
        await service.publish_pending(limit=0)


async def test_pending_query_is_ordered_and_bounded(session: AsyncSession) -> None:
    tenant = Tenant(name="Ordering Tenant")
    session.add(tenant)
    await session.flush()
    repository = OutboxRepository(session)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(3):
        await repository.create_event(
            tenant_id=tenant.id,
            aggregate_type="test",
            aggregate_id=str(index),
            event_type="test.event",
            payload={"index": index},
            occurred_at=start + timedelta(seconds=index),
        )
    await session.commit()

    pending = await repository.list_pending(limit=2)

    assert len(pending) == 2
    assert [event.aggregate_id for event in pending] == ["0", "1"]
    assert len(list(await session.scalars(select(OutboxEvent)))) == 3
