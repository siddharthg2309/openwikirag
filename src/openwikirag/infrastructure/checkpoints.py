"""LangGraph storage lives in its own schema; requests never run DDL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url


def postgres_dsn(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("Durable checkpoints require PostgreSQL.")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def checkpoint_store(url: str) -> AsyncIterator[AsyncPostgresSaver]:
    async with await AsyncConnection.connect(
        postgres_dsn(url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c search_path=ow_checkpoints -c statement_timeout=15000",
    ) as connection:
        yield AsyncPostgresSaver(connection)


async def setup_checkpoints(url: str, *, grant_role: str | None = None) -> None:
    """Operator-only SDK migrations and optional restricted application grants."""
    async with await AsyncConnection.connect(postgres_dsn(url), autocommit=True) as connection:
        await connection.execute("CREATE SCHEMA IF NOT EXISTS ow_checkpoints")
    async with checkpoint_store(url) as saver:
        await saver.setup()
    if grant_role:
        async with await AsyncConnection.connect(postgres_dsn(url), autocommit=True) as connection:
            await connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA ow_checkpoints TO {}").format(
                    sql.Identifier(grant_role)
                )
            )
            await connection.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    "ON ALL TABLES IN SCHEMA ow_checkpoints TO {}"
                ).format(sql.Identifier(grant_role))
            )
