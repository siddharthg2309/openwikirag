from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(database_url: str) -> AsyncEngine:
    """Create an async engine; connection ownership remains with the caller."""

    engine = create_async_engine(database_url, pool_pre_ping=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions that do not expire loaded objects after commit."""

    return async_sessionmaker(engine, expire_on_commit=False)


async def set_tenant_context(session: AsyncSession, tenant_id: UUID) -> None:
    """Set a transaction-local tenant context for PostgreSQL RLS policies.

    SQLite has no equivalent session setting, so isolated unit tests rely on
    repository predicates and foreign-key enforcement instead.
    """

    bind = session.bind
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
