"""Shared Redis-backed request throttling primitives."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError


class RateLimitUnavailable(Exception):
    """Raised when an enabled limiter cannot make a trustworthy decision."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The bounded result of one fixed-window counter operation."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RedisEvalClient(Protocol):
    """The smallest Redis surface required by the limiter."""

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_arguments: str | int,
    ) -> object:
        """Execute one atomic server-side script."""


class RateLimiter(Protocol):
    """Application-facing rate-limiter port."""

    async def check(
        self,
        *,
        route: str,
        peer: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Consume one request and return the quota decision."""


_FIXED_WINDOW_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


class RedisFixedWindowLimiter:
    """Use one atomic Redis counter per route and resolved peer per time window."""

    _key_prefix = "openwikirag:ratelimit:v1:auth:"

    def __init__(self, client: RedisEvalClient) -> None:
        self._client = client

    @classmethod
    def from_url(cls, redis_url: str) -> "RedisFixedWindowLimiter":
        return cls(Redis.from_url(redis_url, decode_responses=True))

    @classmethod
    def key(cls, *, route: str, peer: str) -> str:
        """Return a privacy-preserving, bounded Redis key for one peer."""

        fingerprint = sha256(f"{route}\x00{peer}".encode()).hexdigest()
        return cls._key_prefix + fingerprint

    async def check(
        self,
        *,
        route: str,
        peer: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit settings must be positive.")

        try:
            raw_result = await self._client.eval(
                _FIXED_WINDOW_SCRIPT,
                1,
                self.key(route=route, peer=peer),
                window_seconds,
            )
            count, ttl = self._parse_result(raw_result)
        except (RedisError, OSError, TimeoutError, ValueError, TypeError) as exc:
            raise RateLimitUnavailable("The rate-limit store is unavailable.") from exc

        retry_after = max(ttl, 1)
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(limit - count, 0),
            retry_after_seconds=retry_after,
        )

    @staticmethod
    def _parse_result(result: object) -> tuple[int, int]:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ValueError("The rate-limit store returned an invalid result.")
        count, ttl = result
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or ttl < 0
        ):
            raise ValueError("The rate-limit store returned invalid counters.")
        return count, ttl
