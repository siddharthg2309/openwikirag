"""Replay canonical graph artifacts: python -m openwikirag.graph_cli --tenant UUID."""

import argparse
import asyncio
from uuid import UUID

from openwikirag.application.graph_projection import rebuild_projection
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.neo4j import Neo4jProjection


async def run(tenant_id: UUID, clear: bool, confirmation: UUID | None) -> int:
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    graph = Neo4jProjection.from_settings(settings)
    try:
        await graph.ensure_schema()
        if clear:
            if confirmation != tenant_id:
                raise ValueError("--clear requires --confirm-tenant matching --tenant")
            await graph.clear(tenant_id=tenant_id, confirmed_tenant=confirmation)
        async with create_session_factory(engine)() as session:
            await set_tenant_context(session, tenant_id)
            return await rebuild_projection(tenant_id=tenant_id, session=session, projection=graph)
    finally:
        await graph.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--confirm-tenant", type=UUID)
    args = parser.parse_args()
    if args.clear and args.confirm_tenant != args.tenant:
        parser.error("--clear requires --confirm-tenant matching --tenant")
    count = asyncio.run(run(args.tenant, args.clear, args.confirm_tenant))
    print(f"Projected {count} canonical artifacts.")


if __name__ == "__main__":
    main()
