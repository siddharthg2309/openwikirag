"""Bounded worker loop for the outbox relay and ingestion consumer."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class OutboxRelay(Protocol):
    async def publish_pending(self, *, limit: int) -> Sequence[object]:
        """Publish committed outbox rows and return the delivered events."""


class IngestionRunner(Protocol):
    async def ensure_group(self) -> None:
        """Ensure the configured Redis consumer group exists."""

    async def reclaim_once(self) -> int:
        """Reclaim stale pending messages for this worker."""

    async def consume_once(self) -> int:
        """Consume one bounded batch of new messages."""


class SessionRollback(Protocol):
    async def rollback(self) -> None:
        """Rollback a failed database transaction."""


type WaitForNextCycle = Callable[[asyncio.Event, float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    """Counts emitted by one worker cycle for logs and tests."""

    published: int
    reclaimed: int
    consumed: int

    @property
    def total_work(self) -> int:
        return self.published + self.reclaimed + self.consumed


class WorkerLoop:
    """Run the relay and consumer with bounded polling and safe shutdown."""

    def __init__(
        self,
        *,
        outbox: OutboxRelay,
        ingestion: IngestionRunner,
        session: SessionRollback,
        outbox_batch_size: int,
        idle_poll_interval_seconds: float,
        error_backoff_seconds: float,
        wait_for_next_cycle: WaitForNextCycle | None = None,
    ) -> None:
        if outbox_batch_size < 1:
            raise ValueError("The outbox batch size must be positive.")
        if idle_poll_interval_seconds <= 0 or error_backoff_seconds <= 0:
            raise ValueError("Worker delays must be positive.")
        self._outbox = outbox
        self._ingestion = ingestion
        self._session = session
        self._outbox_batch_size = outbox_batch_size
        self._idle_poll_interval_seconds = idle_poll_interval_seconds
        self._error_backoff_seconds = error_backoff_seconds
        self._wait_for_next_cycle = wait_for_next_cycle or _wait_for_stop_or_timeout

    async def run_once(self) -> WorkerCycleResult:
        """Relay, reclaim, and consume one deterministic bounded cycle."""

        published = await self._outbox.publish_pending(limit=self._outbox_batch_size)
        reclaimed = await self._ingestion.reclaim_once()
        consumed = await self._ingestion.consume_once()
        return WorkerCycleResult(
            published=len(published),
            reclaimed=reclaimed,
            consumed=consumed,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until a stop event is set or cancellation is requested."""

        await self._ingestion.ensure_group()
        logger.info("worker_loop_started")
        while not stop_event.is_set():
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._rollback_after_failure()
                logger.exception("worker_cycle_failed")
                if not stop_event.is_set():
                    await self._wait_for_next_cycle(stop_event, self._error_backoff_seconds)
                continue

            logger.info(
                "worker_cycle_completed",
                published=result.published,
                reclaimed=result.reclaimed,
                consumed=result.consumed,
            )
            if result.total_work == 0 and not stop_event.is_set():
                await self._wait_for_next_cycle(stop_event, self._idle_poll_interval_seconds)

        logger.info("worker_loop_stopped")

    async def _rollback_after_failure(self) -> None:
        try:
            await self._session.rollback()
        except Exception:
            logger.exception("worker_session_rollback_failed")


async def _wait_for_stop_or_timeout(stop_event: asyncio.Event, delay_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        pass
