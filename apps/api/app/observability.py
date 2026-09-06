"""Request correlation and safe structured HTTP access events."""

import re
import time
from collections.abc import Iterable
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openwikirag.core.logging import clear_request_context, set_request_context

logger = structlog.get_logger("openwikirag.api")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def normalize_request_id(value: str | None) -> str:
    """Accept only bounded log-safe ids and generate one for everything else."""

    if value is not None and _REQUEST_ID.fullmatch(value):
        return value
    return uuid4().hex


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


def _replace_request_id(scope: Scope, request_id: str) -> Scope:
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != b"x-request-id"
    ]
    headers.append((b"x-request-id", request_id.encode("ascii")))
    return {**scope, "headers": headers}


def _response_headers(message: Message, request_id: str) -> Message:
    headers = [
        (key, value)
        for key, value in message.get("headers", [])
        if key.lower() != b"x-request-id"
    ]
    headers.append((b"x-request-id", request_id.encode("ascii")))
    return {**message, "headers": headers}


class RequestContextMiddleware:
    """Bind one safe request id for the complete lifetime of an HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = normalize_request_id(
            _header_value(scope.get("headers", []), b"x-request-id")
        )
        scoped = _replace_request_id(scope, request_id)
        set_request_context(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                message = _response_headers(message, request_id)
            await send(message)

        try:
            await self.app(scoped, receive, send_with_request_id)
        except Exception as exc:
            logger.error(
                "http_request_failed",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                outcome="failure",
                error_type=type(exc).__name__,
            )
            raise
        else:
            logger.info(
                "http_request_completed",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                outcome="success" if status_code < 500 else "failure",
            )
        finally:
            clear_request_context()
