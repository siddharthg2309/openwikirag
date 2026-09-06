"""Distributed authentication-route throttling tests."""

from collections import defaultdict
from ipaddress import ip_network

import pytest
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.app.rate_limit import AuthRateLimitMiddleware
from openwikirag.core.config import Settings
from openwikirag.security.rate_limit import (
    RateLimitUnavailable,
    RedisFixedWindowLimiter,
)


class FakeRedis:
    def __init__(self, *, ttl: int = 42, failure: Exception | None = None) -> None:
        self.ttl = ttl
        self.failure = failure
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.calls: list[tuple[str, int, tuple[str | int, ...]]] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_arguments: str | int,
    ) -> object:
        self.calls.append((script, numkeys, keys_and_arguments))
        if self.failure is not None:
            raise self.failure
        key = str(keys_and_arguments[0])
        self.counts[key] += 1
        return [self.counts[key], self.ttl]


def _scope(
    *,
    path: str = "/api/v1/auth/token",
    method: str = "POST",
    client: tuple[str, int] | None = ("127.0.0.1", 5000),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": client,
    }


async def _run(
    middleware: AuthRateLimitMiddleware,
    scope: Scope,
) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    return messages


def _application(calls: list[int]) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return application


def _response_status(messages: list[Message]) -> int:
    return int(messages[0]["status"])


def _response_headers(messages: list[Message]) -> dict[bytes, bytes]:
    return dict(messages[0].get("headers", []))


@pytest.mark.asyncio
async def test_redis_counter_is_atomic_and_returns_bounded_decision() -> None:
    redis = FakeRedis(ttl=17)
    limiter = RedisFixedWindowLimiter(redis)

    first = await limiter.check(
        route="/api/v1/auth/token",
        peer="127.0.0.1",
        limit=2,
        window_seconds=60,
    )
    second = await limiter.check(
        route="/api/v1/auth/token",
        peer="127.0.0.1",
        limit=2,
        window_seconds=60,
    )
    third = await limiter.check(
        route="/api/v1/auth/token",
        peer="127.0.0.1",
        limit=2,
        window_seconds=60,
    )

    assert first.allowed and first.remaining == 1
    assert second.allowed and second.remaining == 0
    assert not third.allowed and third.retry_after_seconds == 17
    assert len(redis.calls) == 3
    script, numkeys, arguments = redis.calls[0]
    assert numkeys == 1
    assert arguments[1] == 60
    assert "INCR" in script and "EXPIRE" in script and "TTL" in script
    assert arguments[0] == limiter.key(route="/api/v1/auth/token", peer="127.0.0.1")
    assert "127.0.0.1" not in str(arguments[0])


@pytest.mark.asyncio
async def test_auth_routes_are_limited_before_downstream_application() -> None:
    redis = FakeRedis(ttl=9)
    limiter = RedisFixedWindowLimiter(redis)
    calls: list[int] = []
    middleware = AuthRateLimitMiddleware(
        _application(calls),
        limiter=limiter,
        limit=1,
        window_seconds=60,
    )

    first = await _run(middleware, _scope())
    second = await _run(middleware, _scope())

    assert _response_status(first) == 200
    first_headers = _response_headers(first)
    assert first_headers[b"x-ratelimit-limit"] == b"1"
    assert first_headers[b"x-ratelimit-remaining"] == b"0"
    assert first_headers[b"x-ratelimit-reset-after"] == b"9"
    assert _response_status(second) == 429
    second_headers = _response_headers(second)
    assert second_headers[b"retry-after"] == b"9"
    assert second_headers[b"x-ratelimit-remaining"] == b"0"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_forwarded_header_does_not_change_direct_peer_key() -> None:
    redis = FakeRedis()
    limiter = RedisFixedWindowLimiter(redis)
    calls: list[int] = []
    middleware = AuthRateLimitMiddleware(
        _application(calls),
        limiter=limiter,
        limit=10,
        window_seconds=60,
    )

    await _run(
        middleware,
        _scope(headers=[(b"x-forwarded-for", b"203.0.113.10")]),
    )
    await _run(
        middleware,
        _scope(headers=[(b"x-forwarded-for", b"203.0.113.11")]),
    )

    assert len(redis.counts) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_allowlisted_proxy_resolves_distinct_client_addresses() -> None:
    redis = FakeRedis()
    limiter = RedisFixedWindowLimiter(redis)
    middleware = AuthRateLimitMiddleware(
        _application([]),
        limiter=limiter,
        limit=10,
        window_seconds=60,
        trusted_proxy_networks=(ip_network("10.0.0.0/8"),),
    )

    await _run(
        middleware,
        _scope(
            client=("10.10.10.10", 5000),
            headers=[(b"x-forwarded-for", b"203.0.113.10")],
        ),
    )
    await _run(
        middleware,
        _scope(
            client=("10.10.10.10", 5000),
            headers=[(b"x-forwarded-for", b"203.0.113.11")],
        ),
    )

    assert len(redis.counts) == 2
    assert limiter.key(route="/api/v1/auth/token", peer="203.0.113.10") in redis.counts
    assert limiter.key(route="/api/v1/auth/token", peer="203.0.113.11") in redis.counts


@pytest.mark.asyncio
async def test_proxy_chain_uses_first_untrusted_hop_from_the_right() -> None:
    redis = FakeRedis()
    limiter = RedisFixedWindowLimiter(redis)
    middleware = AuthRateLimitMiddleware(
        _application([]),
        limiter=limiter,
        limit=10,
        window_seconds=60,
        trusted_proxy_networks=(ip_network("10.0.0.0/8"),),
    )

    await _run(
        middleware,
        _scope(
            client=("10.10.10.10", 5000),
            headers=[(b"x-forwarded-for", b"203.0.113.10, 10.10.10.11")],
        ),
    )

    assert limiter.key(route="/api/v1/auth/token", peer="203.0.113.10") in redis.counts


@pytest.mark.asyncio
async def test_untrusted_peer_and_ambiguous_headers_fall_back_to_direct_peer() -> None:
    redis = FakeRedis()
    limiter = RedisFixedWindowLimiter(redis)
    middleware = AuthRateLimitMiddleware(
        _application([]),
        limiter=limiter,
        limit=10,
        window_seconds=60,
        trusted_proxy_networks=(ip_network("10.0.0.0/8"),),
    )

    for headers, client in [
        ([(b"x-forwarded-for", b"203.0.113.10")], ("198.51.100.10", 5000)),
        ([(b"x-forwarded-for", b"not-an-ip")], ("10.10.10.10", 5000)),
        ([(b"x-forwarded-for", b"10.10.10.11")], ("10.10.10.10", 5000)),
    ]:
        await _run(middleware, _scope(client=client, headers=headers))

    assert limiter.key(route="/api/v1/auth/token", peer="198.51.100.10") in redis.counts
    assert limiter.key(route="/api/v1/auth/token", peer="10.10.10.10") in redis.counts
    assert len(redis.counts) == 2


def test_trusted_proxy_cidr_configuration_is_strict() -> None:
    settings = Settings(trusted_proxy_cidrs="10.0.0.0/8, 2001:db8::/32")
    assert tuple(str(network) for network in settings.trusted_proxy_networks) == (
        "10.0.0.0/8",
        "2001:db8::/32",
    )
    with pytest.raises(ValueError, match="Trusted proxy CIDRs"):
        Settings(trusted_proxy_cidrs="10.0.0.0/8,not-a-network")
    with pytest.raises(ValueError, match="Trusted proxy CIDRs"):
        Settings(trusted_proxy_cidrs="10.0.0.0/8,,10.1.0.0/16")


@pytest.mark.asyncio
async def test_enabled_store_failure_fails_closed_without_calling_application() -> None:
    redis = FakeRedis(failure=RedisError("connection refused"))
    middleware = AuthRateLimitMiddleware(
        _application([]),
        limiter=RedisFixedWindowLimiter(redis),
        limit=10,
        window_seconds=60,
    )

    messages = await _run(middleware, _scope())

    assert _response_status(messages) == 503
    assert messages[1]["body"] == b"Authentication rate limiting is temporarily unavailable."


@pytest.mark.asyncio
async def test_missing_peer_fails_closed() -> None:
    calls: list[int] = []
    middleware = AuthRateLimitMiddleware(
        _application(calls),
        limiter=RedisFixedWindowLimiter(FakeRedis()),
        limit=10,
        window_seconds=60,
    )

    messages = await _run(middleware, _scope(client=None))

    assert _response_status(messages) == 503
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/healthz", "GET"),
        ("/api/v1/auth/token", "GET"),
        ("/api/v1/me", "POST"),
    ],
)
async def test_non_target_requests_do_not_use_redis(path: str, method: str) -> None:
    redis = FakeRedis()
    calls: list[int] = []
    middleware = AuthRateLimitMiddleware(
        _application(calls),
        limiter=RedisFixedWindowLimiter(redis),
        limit=1,
        window_seconds=60,
    )

    messages = await _run(middleware, _scope(path=path, method=method))

    assert _response_status(messages) == 200
    assert not redis.calls
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_malformed_redis_result_is_unavailable() -> None:
    class MalformedRedis:
        async def eval(self, script: str, numkeys: int, *args: str | int) -> object:
            del script, numkeys, args
            return ["one", 10]

    with pytest.raises(RateLimitUnavailable):
        await RedisFixedWindowLimiter(MalformedRedis()).check(
            route="/api/v1/auth/token",
            peer="127.0.0.1",
            limit=1,
            window_seconds=60,
        )
