"""Transport-level request-size boundary tests."""

import pytest
from pydantic import ValidationError
from starlette.types import Message, Receive, Scope, Send

from apps.api.app.request_limits import RequestSizeLimitMiddleware
from openwikirag.core.config import Settings


def _scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {"type": "http", "method": "POST", "path": "/upload", "headers": headers}


async def _run(
    max_request_bytes: int,
    scope: Scope,
    messages: list[Message],
) -> tuple[list[Message], list[bytes]]:
    response_messages: list[Message] = []
    received: list[bytes] = []
    remaining = list(messages)

    async def receive() -> Message:
        if remaining:
            return remaining.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        response_messages.append(message)

    async def application(inner_scope: Scope, inner_receive: Receive, inner_send: Send) -> None:
        while True:
            message = await inner_receive()
            if message.get("type") != "http.request":
                break
            received.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await inner_send({"type": "http.response.start", "status": 200, "headers": []})
        await inner_send({"type": "http.response.body", "body": b"ok"})

    wrapped = RequestSizeLimitMiddleware(application, max_request_bytes=max_request_bytes)
    await wrapped(scope, receive, send)
    return response_messages, received


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_application_runs() -> None:
    app_called = False

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal app_called
        app_called = True

    middleware = RequestSizeLimitMiddleware(application, max_request_bytes=5)
    response_messages: list[Message] = []

    async def receive() -> Message:
        raise AssertionError("declared oversize should not read the body")

    async def send(message: Message) -> None:
        response_messages.append(message)

    await middleware(_scope([(b"content-length", b"6")]), receive, send)

    assert not app_called
    assert response_messages[0]["status"] == 413
    assert response_messages[1]["body"] == b"Request body exceeds the configured limit."


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"-1", b"not-a-number", b"1\n2"])
async def test_invalid_declared_length_is_rejected(value: bytes) -> None:
    middleware = RequestSizeLimitMiddleware(_never, max_request_bytes=5)
    response_messages: list[Message] = []

    async def receive() -> Message:
        raise AssertionError("invalid length should not read the body")

    async def send(message: Message) -> None:
        response_messages.append(message)

    await middleware(_scope([(b"content-length", value)]), receive, send)

    assert response_messages[0]["status"] == 400
    assert response_messages[1]["body"] == b"Invalid request."


@pytest.mark.asyncio
async def test_conflicting_content_lengths_are_rejected() -> None:
    middleware = RequestSizeLimitMiddleware(_never, max_request_bytes=5)
    response_messages: list[Message] = []

    async def receive() -> Message:
        raise AssertionError("conflicting lengths should not read the body")

    async def send(message: Message) -> None:
        response_messages.append(message)

    await middleware(
        _scope([(b"content-length", b"5"), (b"content-length", b"6")]),
        receive,
        send,
    )

    assert response_messages[0]["status"] == 400


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_at_the_first_overflow() -> None:
    response_messages, received = await _run(
        5,
        _scope([]),
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    assert received == [b"abc"]
    assert response_messages[0]["status"] == 413
    assert response_messages[1]["body"] == b"Request body exceeds the configured limit."


@pytest.mark.asyncio
async def test_body_at_or_below_limit_passes_unchanged() -> None:
    response_messages, received = await _run(
        5,
        _scope([]),
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cde", "more_body": False},
        ],
    )

    assert received == [b"ab", b"cde"]
    assert response_messages[0]["status"] == 200
    assert response_messages[1]["body"] == b"ok"


def test_request_bound_must_cover_upload_bound() -> None:
    with pytest.raises(ValidationError, match="HTTP request bound"):
        Settings(max_upload_bytes=10, max_request_bytes=9)


async def _never(scope: Scope, receive: Receive, send: Send) -> None:
    raise AssertionError("application should not be called")
