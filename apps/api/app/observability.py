"""Request correlation and safe structured HTTP access events."""

import re
import time
from collections.abc import Iterable
from uuid import uuid4

import structlog
from opentelemetry.trace import SpanKind, Tracer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openwikirag.core.logging import clear_request_context, set_request_context
from openwikirag.core.metrics import (
    DEFAULT_METRICS,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    MetricsError,
    MetricsRegistry,
)
from openwikirag.core.tracing import (
    extract_trace_context,
    get_tracer,
    inject_traceparent,
    mark_span_error,
    safe_span,
    set_span_attribute,
    span_trace_fields,
)

logger = structlog.get_logger("openwikirag.api")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_METRIC_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_METRIC_ROUTES = frozenset(
    {
        "/",
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/v1/me",
        "/api/v1/auth/register",
        "/api/v1/auth/token",
        "/api/v1/documents",
        "/api/v1/jobs/{job_id}",
        "/api/v1/wiki/pages",
        "/api/v1/wiki/pages/{artifact_id}",
        "/api/v1/wiki/pages/{artifact_id}/review",
        "/api/v1/wiki/pages/{artifact_id}/regenerate",
        "/api/v1/search",
        "/api/v1/answers",
        "/api/v1/answers/{run_id}",
        "/api/v1/answers/{run_id}/trace",
        "/api/v1/answers/{run_id}/execute",
        "/api/v1/answers/{run_id}/stream",
        "/api/v1/conversations",
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/memory",
    }
)


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


def _response_headers(message: Message, request_id: str, traceparent: str | None) -> Message:
    headers = [
        (key, value)
        for key, value in message.get("headers", [])
        if key.lower() not in {b"x-request-id", b"traceparent"}
    ]
    headers.append((b"x-request-id", request_id.encode("ascii")))
    if traceparent is not None:
        headers.append((b"traceparent", traceparent.encode("ascii")))
    return {**message, "headers": headers}


def _metric_route(scope: Scope) -> str:
    route = scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str) and path_format in _METRIC_ROUTES:
        return path_format
    path = scope.get("path")
    if isinstance(path, str) and path in _METRIC_ROUTES:
        return path
    return "unmatched"


def _metric_method(scope: Scope) -> str:
    method = scope.get("method")
    if isinstance(method, str) and method in _METRIC_METHODS:
        return method
    return "OTHER"


def _metric_status_class(status_code: int) -> str:
    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


class RequestContextMiddleware:
    """Bind one safe request id for the complete lifetime of an HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        metrics: MetricsRegistry | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.app = app
        self._metrics = metrics or DEFAULT_METRICS
        self._tracer = tracer or get_tracer()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        parent_context = extract_trace_context(
            _header_value(scope.get("headers", []), b"traceparent")
        )
        with safe_span(
            self._tracer,
            "http.request",
            context=parent_context,
            kind=SpanKind.SERVER,
        ) as span:
            request_id = normalize_request_id(
                _header_value(scope.get("headers", []), b"x-request-id")
            )
            traceparent = inject_traceparent()
            scoped = _replace_request_id(scope, request_id)
            set_request_context(request_id, **span_trace_fields(span))
            started = time.perf_counter()
            status_code = 500

            async def send_with_request_context(message: Message) -> None:
                nonlocal status_code
                if message.get("type") == "http.response.start":
                    status_code = int(message.get("status", 500))
                    message = _response_headers(message, request_id, traceparent)
                await send(message)

            try:
                await self.app(scoped, receive, send_with_request_context)
            except Exception as exc:
                mark_span_error(span, exc)
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
                set_span_attribute(span, "http.request.method", _metric_method(scoped))
                set_span_attribute(span, "http.route", _metric_route(scoped))
                set_span_attribute(span, "http.response.status_code", status_code)
                duration_seconds = max(time.perf_counter() - started, 0.0)
                try:
                    self._metrics.increment(
                        HTTP_REQUESTS_TOTAL,
                        labels={
                            "route": _metric_route(scoped),
                            "method": _metric_method(scoped),
                            "status_class": _metric_status_class(status_code),
                        },
                    )
                    self._metrics.observe(
                        HTTP_REQUEST_DURATION_SECONDS,
                        duration_seconds,
                        labels={
                            "route": _metric_route(scoped),
                            "method": _metric_method(scoped),
                        },
                    )
                except MetricsError:
                    logger.warning("http_metrics_record_failed")
                clear_request_context()
