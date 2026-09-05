"""Redis score reuse and every required scope dimension."""

import os
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from openwikirag.infrastructure.score_cache import CachedScorer


class Scorer:
    identity = "test-model-revision1"
    calls = 0

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        self.calls += 1
        return tuple(0.5 for _ in pairs)


def test_every_scope_dimension_and_passage_changes_key() -> None:
    scope: dict[str, Any] = {
        key: "a" for key in ("tenant", "user", "query", "filters", "generation", "index", "history")
    }
    redis = Redis()
    original = CachedScorer(Scorer(), redis, scope).key((("q", "passage"),))
    for key in scope:
        changed = {**scope, key: "b"}
        assert CachedScorer(Scorer(), redis, changed).key((("q", "passage"),)) != original
    assert CachedScorer(Scorer(), redis, scope).key((("q", "new passage"),)) != original
    assert "passage" not in original


async def test_real_redis_hit_corruption_and_ttl() -> None:
    url = os.getenv("OPENWIKIRAG_TEST_REDIS_URL")
    if not url:
        pytest.skip("real Redis opt-in")
    redis = Redis.from_url(url, socket_timeout=1)
    provider = Scorer()
    cache = CachedScorer(provider, redis, {"tenant": str(uuid4())}, ttl=10)
    pairs = (("query", "canonical passage"),)
    key = cache.key(pairs)
    try:
        assert await cache.score(pairs) == (0.5,)
        assert await cache.score(pairs) == (0.5,)
        assert provider.calls == 1 and 0 < await redis.ttl(key) <= 10
        await redis.set(key, "[NaN]", ex=10)
        assert await cache.score(pairs) == (0.5,)
        assert provider.calls == 2
        await redis.pexpire(key, 1)
        # Deterministic server-side expiry proof without relying on model timing.
        import asyncio

        await asyncio.sleep(0.02)
        assert await cache.score(pairs) == (0.5,)
        assert provider.calls == 3
    finally:
        await redis.delete(key)
        await redis.aclose()
