import logging
import sys

import structlog


def configure_logging(level: str) -> None:
    """Configure structured logs for local development and CI."""

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level.upper(),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level.upper()),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def set_request_context(
    request_id: str,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> None:
    """Bind request-local correlation and safe trace fields to structured logs."""

    structlog.contextvars.clear_contextvars()
    fields: dict[str, str] = {"request_id": request_id}
    if trace_id is not None:
        fields["trace_id"] = trace_id
    if span_id is not None:
        fields["span_id"] = span_id
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    """Prevent request context from leaking across reused async tasks."""

    structlog.contextvars.clear_contextvars()
