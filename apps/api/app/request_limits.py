"""Transport-level HTTP request-size protection."""

import re

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CONTENT_LENGTH = re.compile(r"[0-9]+\Z")


class _RequestTooLarge(Exception):
    """Internal signal used to stop downstream parsing after an overflow."""


def _content_length(scope: Scope) -> tuple[int | None, bool]:
    values: list[str] = []
    for key, value in scope.get("headers", []):
        if key.lower() != b"content-length":
            continue
        try:
            values.append(value.decode("ascii"))
        except UnicodeDecodeError:
            return None, False

    if not values:
        return None, True
    if len(set(values)) != 1:
        return None, False
    value = values[0]
    if not _CONTENT_LENGTH.fullmatch(value):
        return None, False
    try:
        return int(value), True
    except ValueError:
        return None, False


async def _send_rejection(scope: Scope, receive: Receive, send: Send, *, status: int) -> None:
    body = "Request body exceeds the configured limit." if status == 413 else "Invalid request."
    await PlainTextResponse(body, status_code=status)(scope, receive, send)


class RequestSizeLimitMiddleware:
    """Reject oversized bodies before application parsing or buffering."""

    def __init__(self, app: ASGIApp, *, max_request_bytes: int) -> None:
        if max_request_bytes < 1:
            raise ValueError("The maximum request size must be positive.")
        self.app = app
        self._max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared_length, valid_length = _content_length(scope)
        if not valid_length:
            await _send_rejection(scope, receive, send, status=400)
            return
        if declared_length is not None and declared_length > self._max_request_bytes:
            await _send_rejection(scope, receive, send, status=413)
            return

        received = 0

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") != "http.request":
                return message
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                await _send_rejection(scope, receive, send, status=400)
                raise _RequestTooLarge
            received += len(body)
            if received > self._max_request_bytes:
                await _send_rejection(scope, receive, send, status=413)
                raise _RequestTooLarge
            return message

        try:
            await self.app(scope, receive_limited, send)
        except _RequestTooLarge:
            return
