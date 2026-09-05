"""Session-scoped PostgreSQL ownership without lease-expiry checkpoint races."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection

from openwikirag.application.answer_runs import RunBusyError
from openwikirag.infrastructure.checkpoints import postgres_dsn


class PostgresRunMutex:
    def __init__(self, url: str):
        self.url = url

    @asynccontextmanager
    async def hold(self, scope: str) -> AsyncIterator[None]:
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True)
        async with await AsyncConnection.connect(
            postgres_dsn(self.url), autocommit=True, connect_timeout=5
        ) as connection:
            row = await (
                await connection.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            ).fetchone()
            if not row or not row[0]:
                raise RunBusyError("Answer run is already executing.")
            try:
                yield
            finally:
                # Session close is the definitive release, including cancellation.
                await connection.close()
