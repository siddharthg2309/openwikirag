"""ASGI boundary for distributed throttling of unauthenticated auth routes."""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openwikirag.security.rate_limit import RateLimiter, RateLimitUnavailable

_AUTH_ROUTES = frozenset({"/api/v1/auth/register", "/api/v1/auth/token"})


def _direct_peer(scope: Scope) -> str | None:
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return None
    peer = client[0]
    if not isinstance(peer, str) or not peer or len(peer) > 255:
        return None
    return peer


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


async def _send_failure(scope: Scope, receive: Receive, send: Send, *, status: int) -> None:
    if status == 429:
        response = PlainTextResponse(
            "Authentication rate limit exceeded.",
            status_code=status,
        )
    else:
        response = PlainTextResponse(
            "Authentication rate limiting is temporarily unavailable.",
            status_code=status,
        )
    await response(scope, receive, send)


class AuthRateLimitMiddleware:
    """Apply one optional distributed quota before auth request parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter | None,
        limit: int,
        window_seconds: int,
    ) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit settings must be positive.")
        self.app = app
        self._limiter = limiter
        self._limit = limit
        self._window_seconds = window_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or self._limiter is None
            or scope.get("method") != "POST"
            or scope.get("path") not in _AUTH_ROUTES
        ):
            await self.app(scope, receive, send)
            return

        peer = _direct_peer(scope)
        if peer is None:
            await _send_failure(scope, receive, send, status=503)
            return

        route = str(scope["path"])
        try:
            decision = await self._limiter.check(
                route=route,
                peer=peer,
                limit=self._limit,
                window_seconds=self._window_seconds,
            )
        except RateLimitUnavailable:
            await _send_failure(scope, receive, send, status=503)
            return

        headers = _quota_headers(
            limit=decision.limit,
            remaining=decision.remaining,
            retry_after=decision.retry_after_seconds,
        )
        if not decision.allowed:
            headers["Retry-After"] = str(decision.retry_after_seconds)
            response = PlainTextResponse(
                "Authentication rate limit exceeded.",
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
