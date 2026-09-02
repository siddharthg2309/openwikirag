"""Behavioral proof for worker lifecycle and composition boundaries."""

import asyncio
from collections.abc import Awaitable, Callable

from openwikirag.application.worker import WorkerLoop


class FakeOutbox:
    def __init__(self, events: int = 0, failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.calls: list[int] = []

    async def publish_pending(self, *, limit: int) -> list[object]:
        self.calls.append(limit)
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        return [object() for _ in range(self.events)]


class FakeIngestion:
    def __init__(self, stop_event: asyncio.Event | None = None) -> None:
        self.stop_event = stop_event
        self.calls: list[str] = []

    async def ensure_group(self) -> None:
        self.calls.append("ensure_group")

    async def reclaim_once(self) -> int:
        self.calls.append("reclaim")
        return 2

    async def consume_once(self) -> int:
        self.calls.append("consume")
        if self.stop_event is not None:
            self.stop_event.set()
        return 3


class FakeSession:
    def __init__(self) -> None:
        self.rollback_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1


def make_loop(
    outbox: FakeOutbox,
    ingestion: FakeIngestion,
    session: FakeSession,
    *,
    wait_for_next_cycle: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
) -> WorkerLoop:
    return WorkerLoop(
        outbox=outbox,
        ingestion=ingestion,
        session=session,
        outbox_batch_size=7,
        idle_poll_interval_seconds=0.1,
        error_backoff_seconds=0.2,
        wait_for_next_cycle=wait_for_next_cycle,
    )


async def test_cycle_order_and_counts_are_explicit() -> None:
    outbox = FakeOutbox(events=1)
    ingestion = FakeIngestion()
    session = FakeSession()
    worker = make_loop(outbox, ingestion, session)

    result = await worker.run_once()

    assert result.published == 1
    assert result.reclaimed == 2
    assert result.consumed == 3
    assert outbox.calls == [7]
    assert ingestion.calls == ["reclaim", "consume"]


async def test_run_ensures_group_and_stops_at_safe_boundary() -> None:
    stop_event = asyncio.Event()
    outbox = FakeOutbox()
    ingestion = FakeIngestion(stop_event)
    session = FakeSession()
    worker = make_loop(outbox, ingestion, session)

    await worker.run(stop_event)

    assert ingestion.calls == ["ensure_group", "reclaim", "consume"]
    assert session.rollback_calls == 0


async def test_cycle_error_rolls_back_and_uses_error_backoff() -> None:
    stop_event = asyncio.Event()
    outbox = FakeOutbox(failure=RuntimeError("Redis unavailable"))
    ingestion = FakeIngestion()
    session = FakeSession()
    delays: list[float] = []

    async def wait_and_stop(event: asyncio.Event, delay: float) -> None:
        delays.append(delay)
        event.set()

    worker = make_loop(
        outbox,
        ingestion,
        session,
        wait_for_next_cycle=wait_and_stop,
    )

    await worker.run(stop_event)

    assert session.rollback_calls == 1
    assert delays == [0.2]
    assert ingestion.calls == ["ensure_group"]
