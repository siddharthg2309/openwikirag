"""Operator setup: python -m openwikirag.checkpoint_cli [--grant-role openwikirag_app]."""

import argparse
import asyncio

from openwikirag.core.config import get_settings
from openwikirag.infrastructure.checkpoints import setup_checkpoints


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grant-role")
    args = parser.parse_args()
    asyncio.run(
        setup_checkpoints(get_settings().migration_database_url, grant_role=args.grant_role)
    )
    print("Checkpoint schema is ready; request paths do not perform migrations.")


if __name__ == "__main__":
    main()
