"""Request correlation and structured-log redaction tests."""

import json
import re

import pytest
from _pytest.capture import CaptureFixture
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from apps.api.app.main import app
from apps.api.app.observability import RequestContextMiddleware, normalize_request_id


def test_normalize_request_id_rejects_log_unsafe_values() -> None:
    generated = normalize_request_id("line\nbreak")

    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert normalize_request_id("trace-123") == "trace-123"
    assert re.fullmatch(r"[0-9a-f]{32}", normalize_request_id("x" * 129))


@pytest.mark.asyncio
async def test_api_returns_correlation_id_and_redacts_query_from_event(
    capfd: CaptureFixture[str],
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/?token=do-not-log",
            headers={"X-Request-ID": "trace-123"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "trace-123"
    output = capfd.readouterr().out
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    event = next(item for item in events if item.get("event") == "http_request_completed")
    assert event["request_id"] == "trace-123"
    assert event["method"] == "GET"
    assert event["path"] == "/"
    assert event["status_code"] == 200
    assert event["outcome"] == "success"
    assert "do-not-log" not in output


@pytest.mark.asyncio
async def test_api_failure_event_does_not_log_exception_message(
    capfd: CaptureFixture[str],
) -> None:
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("secret-provider-response")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    with pytest.raises(RuntimeError, match="secret-provider-response"):
        await RequestContextMiddleware(failing_app)(
            {"type": "http", "method": "GET", "path": "/boom", "headers": []},
            receive,
            send,
        )

    output = capfd.readouterr().out
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    event = next(item for item in events if item.get("event") == "http_request_failed")
    assert event["error_type"] == "RuntimeError"
    assert event["outcome"] == "failure"
    assert "secret-provider-response" not in output
