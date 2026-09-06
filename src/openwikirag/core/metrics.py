"""Small bounded Prometheus-compatible metrics registry.

The registry intentionally exposes only application-owned metrics. It does not
collect process internals or accept arbitrary label names/values, which keeps
the observability surface predictable and prevents accidental high-cardinality
or sensitive labels.
"""

import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

MetricKind = Literal["counter", "histogram"]
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")


class MetricsError(ValueError):
    """Raised when a metric definition or observation violates its contract."""


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """Immutable definition of one application metric family."""

    name: str
    help: str
    kind: MetricKind
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = ()


@dataclass(slots=True)
class _HistogramSample:
    bucket_counts: list[int]
    count: int = 0
    total: float = 0.0


class MetricsRegistry:
    """Thread-safe in-process counters and histograms with bounded series."""

    def __init__(
        self,
        definitions: Iterable[MetricDefinition],
        *,
        max_series_per_metric: int = 2048,
    ) -> None:
        if max_series_per_metric < 1:
            raise MetricsError("The maximum metric series must be positive.")
        self._definitions: dict[str, MetricDefinition] = {}
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._histograms: dict[str, dict[tuple[str, ...], _HistogramSample]] = {}
        self._max_series_per_metric = max_series_per_metric
        self._lock = threading.RLock()
        for definition in definitions:
            self._register(definition)

    def increment(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
        value: float = 1.0,
    ) -> None:
        """Increment a counter by a finite positive value."""

        if not math.isfinite(value) or value <= 0:
            raise MetricsError("Counter increments must be finite and positive.")
        with self._lock:
            definition, key = self._resolve(name, labels)
            if definition.kind != "counter":
                raise MetricsError(f"Metric {name!r} is not a counter.")
            values = self._counters[name]
            values[key] = values.get(key, 0.0) + value

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Observe one finite non-negative value in a histogram."""

        if not math.isfinite(value) or value < 0:
            raise MetricsError("Histogram observations must be finite and non-negative.")
        with self._lock:
            definition, key = self._resolve(name, labels)
            if definition.kind != "histogram":
                raise MetricsError(f"Metric {name!r} is not a histogram.")
            values = self._histograms[name]
            sample = values.get(key)
            if sample is None:
                sample = _HistogramSample([0] * len(definition.buckets))
                values[key] = sample
            for index, bucket in enumerate(definition.buckets):
                if value <= bucket:
                    sample.bucket_counts[index] += 1
            sample.count += 1
            sample.total += value

    def render(self) -> str:
        """Render all defined metric families in Prometheus text format."""

        with self._lock:
            lines: list[str] = []
            for definition in self._definitions.values():
                lines.append(f"# HELP {definition.name} {definition.help}")
                lines.append(f"# TYPE {definition.name} {definition.kind}")
                if definition.kind == "counter":
                    for key, value in sorted(self._counters[definition.name].items()):
                        lines.append(
                            f"{self._sample_name(definition.name, definition, key)} "
                            f"{_format_number(value)}"
                        )
                else:
                    for key, sample in sorted(self._histograms[definition.name].items()):
                        for bucket, count in zip(
                            definition.buckets,
                            sample.bucket_counts,
                            strict=True,
                        ):
                            bucket_name = self._sample_name(
                                definition.name + "_bucket",
                                definition,
                                key,
                                {"le": _format_number(bucket)},
                            )
                            lines.append(
                                f"{bucket_name} {count}"
                            )
                        inf_bucket_name = self._sample_name(
                            definition.name + "_bucket",
                            definition,
                            key,
                            {"le": "+Inf"},
                        )
                        lines.append(
                            f"{inf_bucket_name} {sample.count}"
                        )
                        lines.append(
                            f"{self._sample_name(definition.name + '_count', definition, key)} "
                            f"{sample.count}"
                        )
                        lines.append(
                            f"{self._sample_name(definition.name + '_sum', definition, key)} "
                            f"{_format_number(sample.total)}"
                        )
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Clear observations; intended for isolated tests and local smoke runs."""

        with self._lock:
            for counter_values in self._counters.values():
                counter_values.clear()
            for histogram_values in self._histograms.values():
                histogram_values.clear()

    def _register(self, definition: MetricDefinition) -> None:
        if not _METRIC_NAME.fullmatch(definition.name):
            raise MetricsError(f"Invalid metric name: {definition.name!r}.")
        if not definition.help or "\n" in definition.help:
            raise MetricsError("Metric help text must be a single non-empty line.")
        if definition.name in self._definitions:
            raise MetricsError(f"Metric {definition.name!r} is already registered.")
        if len(set(definition.label_names)) != len(definition.label_names):
            raise MetricsError("Metric label names must be unique.")
        if any(not _METRIC_NAME.fullmatch(label) for label in definition.label_names):
            raise MetricsError("Metric label names must be valid Prometheus names.")
        if definition.kind == "counter" and definition.buckets:
            raise MetricsError("Counters cannot define histogram buckets.")
        if definition.kind == "histogram":
            if not definition.buckets or tuple(sorted(set(definition.buckets))) != (
                definition.buckets
            ):
                raise MetricsError("Histogram buckets must be finite, sorted, and unique.")
            if any(not math.isfinite(bucket) or bucket <= 0 for bucket in definition.buckets):
                raise MetricsError("Histogram buckets must be finite and positive.")
        self._definitions[definition.name] = definition
        self._counters[definition.name] = {}
        self._histograms[definition.name] = {}

    def _resolve(
        self,
        name: str,
        labels: Mapping[str, str] | None,
    ) -> tuple[MetricDefinition, tuple[str, ...]]:
        definition = self._definitions.get(name)
        if definition is None:
            raise MetricsError(f"Metric {name!r} is not registered.")
        provided = dict(labels or {})
        expected = set(definition.label_names)
        if set(provided) != expected:
            raise MetricsError(f"Labels for {name!r} must be exactly {definition.label_names!r}.")
        values: list[str] = []
        for label_name in definition.label_names:
            value = provided[label_name]
            if not isinstance(value, str) or not value or len(value) > 128:
                raise MetricsError(
                    "Metric label values must be non-empty strings of at most 128 characters."
                )
            if "\r" in value or "\n" in value:
                raise MetricsError("Metric label values cannot contain line breaks.")
            values.append(value)
        key = tuple(values)
        if definition.kind == "counter":
            key_exists = key in self._counters[name]
            series_count = len(self._counters[name])
        else:
            key_exists = key in self._histograms[name]
            series_count = len(self._histograms[name])
        if not key_exists and series_count >= self._max_series_per_metric:
            raise MetricsError(f"Metric {name!r} exceeded its series bound.")
        return definition, key

    @staticmethod
    def _sample_name(
        name: str,
        definition: MetricDefinition,
        key: tuple[str, ...],
        extra: Mapping[str, str] | None = None,
    ) -> str:
        labels = dict(zip(definition.label_names, key, strict=True))
        if extra:
            labels.update(extra)
        if not labels:
            return name
        encoded = ",".join(
            f'{label}="{_escape_label(value)}"' for label, value in labels.items()
        )
        return f"{name}{{{encoded}}}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_number(value: float) -> str:
    return format(value, ".17g")


_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS_TOTAL = "openwikirag_http_requests_total"
HTTP_REQUEST_DURATION_SECONDS = "openwikirag_http_request_duration_seconds"
WORKER_CYCLES_TOTAL = "openwikirag_worker_cycles_total"
WORKER_CYCLE_DURATION_SECONDS = "openwikirag_worker_cycle_duration_seconds"
INGESTION_MESSAGES_TOTAL = "openwikirag_ingestion_messages_total"
INGESTION_MESSAGE_DURATION_SECONDS = "openwikirag_ingestion_message_duration_seconds"

DEFAULT_METRICS = MetricsRegistry(
    (
        MetricDefinition(
            HTTP_REQUESTS_TOTAL,
            "Total HTTP requests handled by the API.",
            "counter",
            ("route", "method", "status_class"),
        ),
        MetricDefinition(
            HTTP_REQUEST_DURATION_SECONDS,
            "HTTP request duration in seconds.",
            "histogram",
            ("route", "method"),
            _LATENCY_BUCKETS,
        ),
        MetricDefinition(
            WORKER_CYCLES_TOTAL,
            "Total worker cycles completed or failed.",
            "counter",
            ("outcome",),
        ),
        MetricDefinition(
            WORKER_CYCLE_DURATION_SECONDS,
            "Worker cycle duration in seconds.",
            "histogram",
            ("outcome",),
            _LATENCY_BUCKETS,
        ),
        MetricDefinition(
            INGESTION_MESSAGES_TOTAL,
            "Total ingestion messages handled by outcome.",
            "counter",
            ("outcome",),
        ),
        MetricDefinition(
            INGESTION_MESSAGE_DURATION_SECONDS,
            "Ingestion message handling duration in seconds.",
            "histogram",
            ("outcome",),
            _LATENCY_BUCKETS,
        ),
    )
)
