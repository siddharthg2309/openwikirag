"""Behavioral proof for bounded Prometheus-compatible metrics."""

import math
from collections.abc import Generator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.app.main import app
from openwikirag.core.metrics import (
    DEFAULT_METRICS,
    MetricDefinition,
    MetricsError,
    MetricsRegistry,
)


@pytest.fixture(autouse=True)
def reset_default_metrics() -> Generator[None]:
    DEFAULT_METRICS.reset()
    yield
    DEFAULT_METRICS.reset()


def test_registry_renders_cumulative_histogram_and_counter() -> None:
    registry = MetricsRegistry(
        (
            MetricDefinition(
                "example_events_total",
                "Example events.",
                "counter",
                ("outcome",),
            ),
            MetricDefinition(
                "example_duration_seconds",
                "Example duration.",
                "histogram",
                ("outcome",),
                (0.1, 1.0),
            ),
        )
    )

    registry.increment("example_events_total", labels={"outcome": "success"}, value=2)
    registry.observe(
        "example_duration_seconds",
        0.05,
        labels={"outcome": "success"},
    )
    registry.observe(
        "example_duration_seconds",
        0.5,
        labels={"outcome": "success"},
    )

    output = registry.render()

    assert "# TYPE example_events_total counter" in output
    assert 'example_events_total{outcome="success"} 2' in output
    assert 'example_duration_seconds_bucket{outcome="success",le="0.10000000000000001"} 1' in output
    assert 'example_duration_seconds_bucket{outcome="success",le="1"} 2' in output
    assert 'example_duration_seconds_bucket{outcome="success",le="+Inf"} 2' in output
    assert 'example_duration_seconds_count{outcome="success"} 2' in output
    assert 'example_duration_seconds_sum{outcome="success"} 0.55000000000000004' in output


def test_registry_rejects_unknown_labels_and_nonfinite_values() -> None:
    registry = MetricsRegistry(
        (MetricDefinition("events_total", "Events.", "counter", ("outcome",)),)
    )

    with pytest.raises(MetricsError):
        registry.increment("events_total", labels={"tenant_id": str(uuid4())})
    with pytest.raises(MetricsError):
        registry.increment("events_total", labels={"outcome": "success"}, value=math.nan)


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_request_metrics_without_sensitive_labels() -> None:
    document_id = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        health = await client.get("/healthz")
        await client.get(f"/api/v1/jobs/{document_id}?tenant_id=private-query")
        response = await client.get("/metrics")

    assert health.status_code == 200
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE openwikirag_http_requests_total counter" in response.text
    assert 'route="/healthz"' in response.text
    assert 'status_class="2xx"' in response.text
    assert document_id.hex not in response.text
    assert "private-query" not in response.text
    assert 'tenant_id=' not in response.text
