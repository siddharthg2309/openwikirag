"""Operator purge: python -m openwikirag.retention_cli [--batch 100]."""

import argparse
import asyncio
from datetime import UTC, datetime

from openwikirag.application.retention import purge_due
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.checkpoints import checkpoint_store
from openwikirag.infrastructure.database import create_database_engine, create_session_factory


async def run(batch: int) -> tuple[int, int, int]:
    settings = get_settings()
    engine = create_database_engine(settings.migration_database_url)
    try:
        async with checkpoint_store(settings.migration_database_url) as saver:
            async with create_session_factory(engine)() as session:
                result = await purge_due(session, saver, now=datetime.now(UTC), batch=batch)
                await session.commit()
                return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()
    conversations, memories, runs = asyncio.run(run(args.batch))
    print(f"Purged conversations={conversations} memories={memories} checkpoints={runs}")


if __name__ == "__main__":
    main()
