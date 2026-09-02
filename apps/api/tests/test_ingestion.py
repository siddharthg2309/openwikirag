import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.application.ingestion import (
    IngestionConsumerService,
    PermanentJobError,
    RetryableJobError,
)
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    Base,
    Document,
    DocumentVersion,
    IngestionJob,
    Tenant,
)
from openwikirag.infrastructure.streams import StreamMessage


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ingestion.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    async with session_factory() as test_session:
        yield test_session
    await engine.dispose()


class FakeTransport:
    def __init__(self) -> None:
        self.new_messages: list[StreamMessage] = []
        self.stale_messages: list[StreamMessage] = []
        self.acknowledged: list[str] = []
        self.dead_letters: list[tuple[StreamMessage, str]] = []
        self.group_calls = 0

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
        raise AssertionError("The consumer should not publish normal events.")

    async def ensure_group(self, *, stream_name: str, group_name: str) -> None:
        self.group_calls += 1

    async def read(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        count: int,
        block_ms: int,
    ) -> list[StreamMessage]:
        messages = self.new_messages[:count]
        del self.new_messages[:count]
        return messages

    async def claim_stale(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamMessage]:
        messages = self.stale_messages[:count]
        del self.stale_messages[:count]
        return messages

    async def acknowledge(
        self,
        *,
        stream_name: str,
        group_name: str,
        message_id: str,
    ) -> None:
        self.acknowledged.append(message_id)

    async def publish_dead_letter(
        self,
        *,
        stream_name: str,
        message: StreamMessage,
        reason: str,
    ) -> str:
        self.dead_letters.append((message, reason))
        return f"dead-{len(self.dead_letters)}"


class RecordingHandler:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[UUID] = []

    async def handle(self, *, job_id: UUID, payload: dict[str, object]) -> None:
        self.calls.append(job_id)
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure


async def create_job(session: AsyncSession) -> tuple[Tenant, IngestionJob]:
    tenant = Tenant(name="Ingestion Tenant")
    session.add(tenant)
    await session.flush()
    document = Document(tenant_id=tenant.id, title="Notes", source_type="text")
    session.add(document)
    await session.flush()
    version = DocumentVersion(
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=1,
        original_filename="notes.txt",
        sanitized_filename="notes.txt",
        source_type="text",
        media_type="text/plain",
        byte_size=5,
        checksum_sha256="a" * 64,
        source_object_key="tenants/source",
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id
    job = IngestionJob(tenant_id=tenant.id, document_version_id=version.id)
    session.add(job)
    await session.commit()
    return tenant, job


def make_message(tenant_id: UUID, job_id: UUID, *, message_id: str = "1-0") -> StreamMessage:
    payload: dict[str, object] = {
        "document_id": str(uuid4()),
        "document_version_id": str(uuid4()),
        "ingestion_job_id": str(job_id),
    }
    return StreamMessage(
        message_id=message_id,
        fields={
            "event_id": str(uuid4()),
            "event_type": "document.ingestion.requested",
            "tenant_id": str(tenant_id),
            "aggregate_id": str(job_id),
            "payload": json.dumps(payload),
        },
    )


def make_service(
    session: AsyncSession,
    transport: FakeTransport,
    handler: RecordingHandler,
    *,
    lease_seconds: int = 60,
) -> IngestionConsumerService:
    return IngestionConsumerService(
        session,
        transport,
        handler,
        stream_name="openwikirag:test-ingestion",
        group_name="test-group",
        consumer_name="test-consumer",
        dead_letter_stream_name="openwikirag:test-dead-letter",
        lease_seconds=lease_seconds,
        retry_backoff_base_seconds=5,
        retry_backoff_max_seconds=60,
        batch_size=10,
        block_ms=1,
    )


async def test_success_commits_before_ack_and_terminal_duplicate_skips_handler(
    session: AsyncSession,
) -> None:
    tenant, job = await create_job(session)
    job_id = job.id
    transport = FakeTransport()
    handler = RecordingHandler()
    message = make_message(tenant.id, job_id)
    transport.new_messages.append(message)
    service = make_service(session, transport, handler)

    await service.ensure_group()
    assert await service.consume_once() == 1
    transport.new_messages.append(message)
    assert await service.consume_once() == 1

    refreshed = await session.get(IngestionJob, job_id)
    assert refreshed is not None
    assert refreshed.status == "succeeded"
    assert refreshed.started_at is not None
    assert refreshed.completed_at is not None
    assert handler.calls == [job_id]
    assert transport.acknowledged == ["1-0", "1-0"]
    assert transport.group_calls == 1


async def test_retryable_failure_sets_backoff_and_reclaim_can_finish_job(
    session: AsyncSession,
) -> None:
    tenant, job = await create_job(session)
    transport = FakeTransport()
    handler = RecordingHandler(RetryableJobError("temporary provider failure"))
    message = make_message(tenant.id, job.id)
    transport.new_messages.append(message)
    service = make_service(session, transport, handler, lease_seconds=1)

    before = datetime.now(UTC)
    assert await service.consume_once() == 1
    after = datetime.now(UTC)
    retrying = await session.get(IngestionJob, job.id)
    assert retrying is not None
    assert retrying.status == "retryable"
    assert retrying.attempts == 1
    assert retrying.available_at >= before + timedelta(seconds=5)
    assert retrying.available_at <= after + timedelta(seconds=5, milliseconds=100)
    assert transport.acknowledged == []

    retrying.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    transport.stale_messages.append(message)
    assert await service.reclaim_once() == 1

    finished = await session.get(IngestionJob, job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.attempts == 2
    assert handler.calls == [job.id, job.id]
    assert transport.acknowledged == ["1-0"]


async def test_permanent_failure_is_dead_lettered_before_ack(
    session: AsyncSession,
) -> None:
    tenant, job = await create_job(session)
    transport = FakeTransport()
    handler = RecordingHandler(PermanentJobError("unsupported document"))
    transport.new_messages.append(make_message(tenant.id, job.id))
    service = make_service(session, transport, handler)

    await service.consume_once()

    refreshed = await session.get(IngestionJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "dead_letter"
    assert refreshed.last_error_code == "INGESTION_PERMANENT_FAILURE"
    assert transport.dead_letters[0][1] == "INGESTION_PERMANENT_FAILURE"
    assert transport.acknowledged == ["1-0"]


async def test_malformed_event_is_dead_lettered_without_job_mutation(
    session: AsyncSession,
) -> None:
    tenant, job = await create_job(session)
    transport = FakeTransport()
    malformed = StreamMessage(message_id="2-0", fields={"event_type": "unknown"})
    transport.new_messages.append(malformed)
    handler = RecordingHandler()
    service = make_service(session, transport, handler)

    await service.consume_once()

    refreshed = await session.get(IngestionJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert handler.calls == []
    assert transport.dead_letters[0][1] == "MALFORMED_EVENT"
    assert transport.acknowledged == ["2-0"]


async def test_active_lease_is_not_processed_again(session: AsyncSession) -> None:
    tenant, job = await create_job(session)
    job.status = "running"
    job.attempts = 1
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await session.commit()
    transport = FakeTransport()
    transport.new_messages.append(make_message(tenant.id, job.id))
    handler = RecordingHandler()
    service = make_service(session, transport, handler)

    await service.consume_once()

    assert handler.calls == []
    assert transport.acknowledged == []


async def test_exhausted_job_is_dead_lettered_and_acknowledged(session: AsyncSession) -> None:
    tenant, job = await create_job(session)
    job.status = "retryable"
    job.attempts = job.max_attempts
    job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    transport = FakeTransport()
    transport.new_messages.append(make_message(tenant.id, job.id))
    service = make_service(session, transport, RecordingHandler())

    await service.consume_once()

    refreshed = await session.get(IngestionJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "dead_letter"
    assert transport.dead_letters[0][1] == "MAX_ATTEMPTS_EXCEEDED"
    assert transport.acknowledged == ["1-0"]
