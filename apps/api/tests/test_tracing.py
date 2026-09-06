"""OpenTelemetry context and HTTP span tests."""

from collections.abc import Sequence
from typing import cast

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Tracer
from starlette.types import Message, Receive, Scope, Send

from apps.api.app.observability import RequestContextMiddleware
from openwikirag.core.tracing import extract_trace_context, inject_traceparent, safe_span


class RecordingSpanExporter(SpanExporter):
    """Synchronous exporter used to prove parentage without an external collector."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def _tracer() -> tuple[TracerProvider, RecordingSpanExporter]:
    provider = TracerProvider()
    exporter = RecordingSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _span(exporter: RecordingSpanExporter, name: str) -> ReadableSpan:
    return next(span for span in exporter.spans if span.name == name)


def test_w3c_traceparent_round_trip_preserves_parentage() -> None:
    provider, exporter = _tracer()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("request"):
        traceparent = inject_traceparent()
        assert traceparent is not None
        with tracer.start_as_current_span(
            "job",
            context=extract_trace_context(traceparent),
        ):
            pass

    request = _span(exporter, "request")
    job = _span(exporter, "job")
    assert job.parent is not None
    assert request.context.trace_id == job.context.trace_id
    assert job.parent.span_id == request.context.span_id

    with tracer.start_as_current_span(
        "invalid-parent",
        context=extract_trace_context("x" * 257),
    ):
        pass
    assert _span(exporter, "invalid-parent").parent is None


def test_trace_provider_failure_falls_back_to_non_recording_span() -> None:
    class FailingTracer:
        def start_as_current_span(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("provider unavailable")

    with safe_span(
        cast(Tracer, FailingTracer()),
        "provider-failure",
        context=Context(),
        kind=SpanKind.INTERNAL,
    ) as span:
        assert not span.get_span_context().is_valid


@pytest.mark.asyncio
async def test_http_span_uses_incoming_parent_and_returns_traceparent() -> None:
    provider, exporter = _tracer()
    tracer = provider.get_tracer("test")
    response_messages: list[Message] = []

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        response_messages.append(message)

    with tracer.start_as_current_span("caller") as caller:
        incoming = inject_traceparent()
        assert incoming is not None
        await RequestContextMiddleware(application, tracer=tracer)(
            {
                "type": "http",
                "method": "GET",
                "path": "/healthz",
                "headers": [(b"traceparent", incoming.encode("ascii"))],
            },
            receive,
            send,
        )

    http_span = _span(exporter, "http.request")
    assert http_span.parent is not None
    assert http_span.parent.span_id == caller.get_span_context().span_id
    assert http_span.attributes is not None
    assert http_span.attributes["http.route"] == "/healthz"

    response_start = response_messages[0]
    response_headers = dict(response_start["headers"])
    returned = response_headers[b"traceparent"].decode("ascii")
    returned_context = extract_trace_context(returned)
    returned_span = trace.get_current_span(returned_context).get_span_context()
    assert returned_span.trace_id == http_span.context.trace_id
    assert returned_span.span_id == http_span.context.span_id
