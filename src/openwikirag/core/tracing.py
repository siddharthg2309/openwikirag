"""Small OpenTelemetry foundation for safe request-to-job tracing."""

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import (
    INVALID_SPAN_CONTEXT,
    NonRecordingSpan,
    Span,
    SpanKind,
    StatusCode,
    Tracer,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_INSTRUMENTATION_NAME: Final = "openwikirag"
_MAX_TRACEPARENT_BYTES: Final = 128
_TRACEPARENT = re.compile(
    r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}(?:-[0-9a-f]{2,})?\Z"
)
_PROPAGATOR = TraceContextTextMapPropagator()
_CONFIGURED = False


def configure_tracing(*, service_name: str) -> None:
    """Install one safe SDK provider for a deployable process.

    No exporter is installed by default. A deployment can later add an
    exporter without changing domain code; local tests supply their own
    tracer and in-memory exporter so span behavior remains deterministic.
    """

    global _CONFIGURED
    if _CONFIGURED:
        return
    trace.set_tracer_provider(
        TracerProvider(resource=Resource.create({"service.name": service_name}))
    )
    _CONFIGURED = True


def get_tracer() -> Tracer:
    """Return the shared instrumentation tracer."""

    return trace.get_tracer(_INSTRUMENTATION_NAME)


def extract_trace_context(traceparent: str | None) -> Context:
    """Extract a bounded W3C parent, returning a root context on bad input."""

    if traceparent is None:
        return Context()
    try:
        if len(traceparent.encode("ascii")) > _MAX_TRACEPARENT_BYTES:
            return Context()
    except UnicodeEncodeError:
        return Context()
    if not _TRACEPARENT.fullmatch(traceparent):
        return Context()
    try:
        return _PROPAGATOR.extract(carrier={"traceparent": traceparent}, context=Context())
    except (TypeError, ValueError):
        return Context()


def inject_traceparent(context: Context | None = None) -> str | None:
    """Serialize the active context as a safe W3C traceparent value."""

    carrier: dict[str, str] = {}
    try:
        _PROPAGATOR.inject(carrier=carrier, context=context)
    except (TypeError, ValueError):
        return None
    traceparent = carrier.get("traceparent")
    if traceparent is None or not _TRACEPARENT.fullmatch(traceparent):
        return None
    return traceparent


def current_traceparent() -> str | None:
    """Return the active span context for durable handoff metadata."""

    return inject_traceparent()


@contextmanager
def safe_span(
    tracer: Tracer,
    name: str,
    *,
    context: Context,
    kind: SpanKind,
) -> Iterator[Span]:
    """Start/end a span without allowing tracing failures to affect the caller."""

    manager = None
    try:
        manager = tracer.start_as_current_span(
            name,
            context=context,
            kind=kind,
            record_exception=False,
            set_status_on_exception=False,
        )
        span = manager.__enter__()
    except Exception:
        yield NonRecordingSpan(INVALID_SPAN_CONTEXT)
        return

    try:
        yield span
    except BaseException:
        try:
            assert manager is not None
            manager.__exit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        try:
            assert manager is not None
            manager.__exit__(None, None, None)
        except Exception:
            pass


def set_span_attribute(span: Span, key: str, value: str | int | float | bool) -> None:
    """Set one bounded attribute while containing provider-specific failures."""

    try:
        span.set_attribute(key, value)
    except Exception:
        pass


def span_trace_fields(span: Span) -> dict[str, str]:
    """Return only safe identifiers for structured-log binding."""

    context = span.get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def mark_span_error(span: Span, error: BaseException) -> None:
    """Record failure classification without exporting the exception message."""

    try:
        span.set_status(StatusCode.ERROR)
        set_span_attribute(span, "error.type", type(error).__name__)
    except Exception:
        pass


__all__ = [
    "SpanKind",
    "configure_tracing",
    "current_traceparent",
    "extract_trace_context",
    "get_tracer",
    "inject_traceparent",
    "mark_span_error",
    "safe_span",
    "set_span_attribute",
    "span_trace_fields",
]
