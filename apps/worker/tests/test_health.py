"""Socket-level proof for the worker liveness/readiness contract."""

import asyncio
import json

import pytest

from apps.worker.app.health import WorkerHealthServer


async def _request(port: int, request: bytes) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        response = await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()

    header, body = response.split(b"\r\n\r\n", 1)
    status = int(header.split(b" ", 2)[1])
    return status, json.loads(body)


@pytest.mark.asyncio
async def test_worker_health_separates_liveness_from_readiness() -> None:
    server = WorkerHealthServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        health_status, health_body = await _request(
            server.bound_port,
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        ready_status, ready_body = await _request(
            server.bound_port,
            b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        assert health_status == 200
        assert health_body == {"status": "ok", "service": "openwikirag-worker"}
        assert ready_status == 503
        assert ready_body == {"status": "not_ready", "service": "openwikirag-worker"}

        server.mark_ready()
        ready_status, ready_body = await _request(
            server.bound_port,
            b"GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        assert ready_status == 200
        assert ready_body == {
            "status": "ready",
            "service": "openwikirag-worker",
            "checks": {"initialization": "ok"},
        }
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_request",
    [
        b"POST /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /healthz\r\n",
        b"GET /healthz HTTP/1.1\r\nHost: " + b"x" * 2048,
    ],
)
async def test_worker_health_rejects_unsupported_or_oversized_requests(
    raw_request: bytes,
) -> None:
    server = WorkerHealthServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        status, _ = await _request(server.bound_port, raw_request)
        assert status in {400, 404}
    finally:
        await server.close()


def test_worker_health_configuration_is_bounded() -> None:
    with pytest.raises(ValueError):
        WorkerHealthServer(host="", port=8001)
    with pytest.raises(ValueError):
        WorkerHealthServer(host="127.0.0.1", port=-1)
    with pytest.raises(ValueError):
        WorkerHealthServer(host="127.0.0.1", port=8001, max_request_bytes=0)
