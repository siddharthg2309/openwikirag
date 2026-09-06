"""ASGI boundaries for distributed API request throttling."""

from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openwikirag.security.rate_limit import RateLimiter, RateLimitUnavailable

_AUTH_ROUTES = frozenset({"/api/v1/auth/register", "/api/v1/auth/token"})
_MAX_FORWARDED_HOPS = 16
_IPNetwork = IPv4Network | IPv6Network
_IPAddress = IPv4Address | IPv6Address


def _direct_peer(scope: Scope) -> str | None:
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return None
    peer = client[0]
    if not isinstance(peer, str) or not peer or len(peer) > 255:
        return None
    return peer


def _parse_ip(value: str) -> _IPAddress | None:
    try:
        return ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(address: _IPAddress, networks: Sequence[_IPNetwork]) -> bool:
    return any(address in network for network in networks)


def _forwarded_addresses(scope: Scope) -> tuple[_IPAddress, ...] | None:
    values: list[str] = []
    for key, value in scope.get("headers", []):
        if key.lower() != b"x-forwarded-for":
            continue
        try:
            values.extend(value.decode("ascii").split(","))
        except UnicodeDecodeError:
            return None
    if not values:
        return ()
    if len(values) > _MAX_FORWARDED_HOPS:
        return None
    parsed = tuple(_parse_ip(value) for value in values)
    if any(address is None for address in parsed):
        return None
    return tuple(address for address in parsed if address is not None)


def _client_identity(scope: Scope, trusted_proxy_networks: Sequence[_IPNetwork]) -> str | None:
    """Resolve one limiter identity without trusting arbitrary forwarded headers."""

    direct_peer = _direct_peer(scope)
    if direct_peer is None or not trusted_proxy_networks:
        return direct_peer

    direct_address = _parse_ip(direct_peer)
    if direct_address is None or not _is_trusted(direct_address, trusted_proxy_networks):
        return direct_peer

    forwarded = _forwarded_addresses(scope)
    if not forwarded:
        return direct_peer
    for address in reversed(forwarded):
        if not _is_trusted(address, trusted_proxy_networks):
            return str(address)
    return direct_peer


def _quota_headers(*, limit: int, remaining: int, retry_after: int) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(remaining, 0)),
        "X-RateLimit-Reset-After": str(max(retry_after, 1)),
    }


def _with_headers(message: Message, headers: dict[str, str]) -> Message:
    existing = [
        (key, value)
        for key, value in message.get("headers", [])
        if key.lower() not in {name.lower().encode("ascii") for name in headers}
    ]
    existing.extend(
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in headers.items()
    )
    return {**message, "headers": existing}


async def _send_failure(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status: int,
    exceeded_message: str,
    unavailable_message: str,
) -> None:
    if status == 429:
        response = PlainTextResponse(
            exceeded_message,
            status_code=status,
        )
    else:
        response = PlainTextResponse(
            unavailable_message,
            status_code=status,
        )
    await response(scope, receive, send)


class _RateLimitMiddleware:
    """Apply one optional distributed quota to a route subset."""

    _exceeded_message = "Rate limit exceeded."
    _unavailable_message = "Rate limiting is temporarily unavailable."

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter | None,
        limit: int,
        window_seconds: int,
        trusted_proxy_networks: Sequence[_IPNetwork] = (),
    ) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit settings must be positive.")
        self.app = app
        self._limiter = limiter
        self._limit = limit
        self._window_seconds = window_seconds
        self._trusted_proxy_networks = tuple(trusted_proxy_networks)

    def _target_route(self, scope: Scope) -> str | None:
        del scope
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = self._target_route(scope)
        if self._limiter is None or route is None:
            await self.app(scope, receive, send)
            return

        peer = _client_identity(scope, self._trusted_proxy_networks)
        if peer is None:
            await _send_failure(
                scope,
                receive,
                send,
                status=503,
                exceeded_message=self._exceeded_message,
                unavailable_message=self._unavailable_message,
            )
            return

        try:
            decision = await self._limiter.check(
                route=route,
                peer=peer,
                limit=self._limit,
                window_seconds=self._window_seconds,
            )
        except RateLimitUnavailable:
            await _send_failure(
                scope,
                receive,
                send,
                status=503,
                exceeded_message=self._exceeded_message,
                unavailable_message=self._unavailable_message,
            )
            return

        headers = _quota_headers(
            limit=decision.limit,
            remaining=decision.remaining,
            retry_after=decision.retry_after_seconds,
        )
        if not decision.allowed:
            headers["Retry-After"] = str(decision.retry_after_seconds)
            response = PlainTextResponse(
                self._exceeded_message,
                status_code=429,
                headers=headers,
            )
            await response(scope, receive, send)
            return

        async def send_with_quota(message: Message) -> None:
            if message.get("type") == "http.response.start":
                message = _with_headers(message, headers)
            await send(message)

        await self.app(scope, receive, send_with_quota)


class AuthRateLimitMiddleware(_RateLimitMiddleware):
    """Apply one optional distributed quota before auth request parsing."""

    _exceeded_message = "Authentication rate limit exceeded."
    _unavailable_message = "Authentication rate limiting is temporarily unavailable."

    def _target_route(self, scope: Scope) -> str | None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _AUTH_ROUTES
        ):
            return None
        return str(scope["path"])


class ApiRateLimitMiddleware(_RateLimitMiddleware):
    """Apply one optional distributed quota to non-authentication API routes."""

    _exceeded_message = "API rate limit exceeded."
    _unavailable_message = "API rate limiting is temporarily unavailable."

    def _target_route(self, scope: Scope) -> str | None:
        path = scope.get("path")
        if (
            scope.get("type") != "http"
            or not isinstance(path, str)
            or not path.startswith("/api/v1/")
            or path in _AUTH_ROUTES
        ):
            return None
        return path
