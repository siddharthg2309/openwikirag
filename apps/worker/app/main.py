import argparse
import asyncio
import signal
from uuid import UUID

import structlog

from openwikirag import __version__
from openwikirag.application.ingestion import (
    IngestionConsumerService,
    PermanentJobError,
)
from openwikirag.application.outbox import OutboxPublisherService
from openwikirag.application.worker import WorkerLoop
from openwikirag.core.config import get_settings
from openwikirag.core.logging import configure_logging
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.streams import RedisStreamPublisher


class DeferredIngestionHandler:
    """Prevent the worker from claiming extraction succeeded before Phase 3."""

    async def handle(self, *, job_id: UUID, payload: dict[str, object]) -> None:
        del job_id, payload
        raise PermanentJobError("The extraction pipeline is not implemented yet.")


async def run_worker(*, stop_event: asyncio.Event | None = None, once: bool = False) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger(service="openwikirag-worker")
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    transport = RedisStreamPublisher.from_url(settings.redis_url)
    try:
        async with session_factory() as session:
            outbox = OutboxPublisherService(
                session,
                transport,
                stream_name=settings.ingestion_stream_name,
            )
            ingestion = IngestionConsumerService(
                session,
                transport,
                DeferredIngestionHandler(),
                stream_name=settings.ingestion_stream_name,
                group_name=settings.ingestion_consumer_group,
                consumer_name=settings.ingestion_consumer_name,
                dead_letter_stream_name=settings.dead_letter_stream_name,
                lease_seconds=settings.job_lease_seconds,
                retry_backoff_base_seconds=settings.job_retry_backoff_base_seconds,
                retry_backoff_max_seconds=settings.job_retry_backoff_max_seconds,
                batch_size=settings.outbox_batch_size,
            )
            worker = WorkerLoop(
                outbox=outbox,
                ingestion=ingestion,
                session=session,
                outbox_batch_size=settings.outbox_batch_size,
                idle_poll_interval_seconds=settings.worker_poll_interval_seconds,
                error_backoff_seconds=settings.worker_error_backoff_seconds,
            )
            if once:
                await ingestion.ensure_group()
                result = await worker.run_once()
                logger.info(
                    "worker_once_completed",
                    published=result.published,
                    reclaimed=result.reclaimed,
                    consumed=result.consumed,
                )
            else:
                await worker.run(stop_event or asyncio.Event())
    finally:
        await transport.close()
        await engine.dispose()


async def _run_from_cli(*, once: bool) -> None:
    stop_event = asyncio.Event()
    if not once:
        loop = asyncio.get_running_loop()
        for interrupt in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(interrupt, stop_event.set)
            except NotImplementedError:
                pass
    await run_worker(stop_event=stop_event, once=once)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OpenWikiRAG worker.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one relay/reclaim/consume cycle and exit.",
    )
    args = parser.parse_args()
    logger = structlog.get_logger(service="openwikirag-worker")
    logger.info("worker_starting", version=__version__, mode="once" if args.once else "loop")
    asyncio.run(_run_from_cli(once=args.once))


if __name__ == "__main__":
    main()
