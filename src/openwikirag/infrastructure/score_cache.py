"""Short-lived owner-scoped score cache; stores no source or conversation text."""

import hashlib
import json
import math
from typing import Any

from redis.asyncio import Redis

from openwikirag.application.reranking import PairwiseReranker


class CachedScorer:
    def __init__(
        self, provider: PairwiseReranker, redis: Redis, scope: dict[str, Any], *, ttl: int = 300
    ):
        if not 1 <= ttl <= 300:
            raise ValueError("Invalid cache TTL.")
        self.provider, self.redis, self.scope, self.ttl = provider, redis, scope, ttl

    @property
    def identity(self) -> str:
        return self.provider.identity

    def key(self, pairs: tuple[tuple[str, str], ...]) -> str:
        encoded = json.dumps(
            {"scope": self.scope, "model": self.identity, "pairs": pairs},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return "openwikirag:score:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def valid(scores: object, count: int) -> bool:
        return (
            isinstance(scores, (tuple, list))
            and len(scores) == count
            and all(type(value) in (float, int) and math.isfinite(value) for value in scores)
        )

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        key = self.key(pairs)
        try:
            raw = await self.redis.getrange(key, 0, 8192)
            if raw and len(raw) <= 8192:
                scores = json.loads(raw)
                if self.valid(scores, len(pairs)):
                    return tuple(float(value) for value in scores)
        except Exception:
            pass  # Optional cache outage does not disable the model.
        result = await self.provider.score(pairs)
        if self.valid(result, len(pairs)):
            try:
                await self.redis.set(key, json.dumps(result, allow_nan=False), ex=self.ttl)
            except Exception:
                pass
        return result
