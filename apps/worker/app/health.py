"""Bounded liveness and readiness probes for the worker process."""

import asyncio
import json

_DEFAULT_MAX_REQUEST_BYTES = 2048
_READ_TIMEOUT_SECONDS = 1.0


class WorkerHealthServer:
    """Serve only minimal health responses on a dedicated worker port."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
    ) -> None:
        if not host.strip() or port < 0 or port > 65_535:
            raise ValueError("Worker health host and port are invalid.")
        if max_request_bytes < 1:
            raise ValueError("Worker health request bound must be positive.")
        self._host = host
        self._port = port
        self._max_request_bytes = max_request_bytes
        self._ready = False
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int:
        """Return the bound port, including an ephemeral test port."""

        if self._server is None or not self._server.sockets:
            raise RuntimeError("Worker health server is not running.")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Bind the listener without claiming dependency readiness."""

        if self._server is not None:
            raise RuntimeError("Worker health server is already running.")
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )

    def mark_ready(self) -> None:
        """Mark readiness after the worker's dependency setup succeeds."""

        if self._server is None:
            raise RuntimeError("Worker health server is not running.")
        self._ready = True

    async def close(self) -> None:
        """Stop accepting probes and close the listener."""

        if self._server is None:
            return
        self._ready = False
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                request = await asyncio.wait_for(
                    reader.read(self._max_request_bytes + 1),
                    timeout=_READ_TIMEOUT_SECONDS,
                )
            except (TimeoutError, ConnectionError, OSError):
                return
            status, payload = self._response(request)
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            reason = {
                200: "OK",
                400: "Bad Request",
                404: "Not Found",
                503: "Service Unavailable",
            }[status]
            response = (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Connection: close\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode("ascii") + body
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _response(self, request: bytes) -> tuple[int, dict[str, object]]:
        if not request or len(request) > self._max_request_bytes:
            return 400, {"status": "invalid_request"}
        request_line = request.split(b"\r\n", 1)[0]
        parts = request_line.split(b" ")
        if len(parts) != 3 or parts[2] not in {b"HTTP/1.0", b"HTTP/1.1"}:
            return 400, {"status": "invalid_request"}
        method, path = parts[0], parts[1]
        if method != b"GET":
            return 400, {"status": "invalid_request"}
        if path == b"/healthz":
            return 200, {"status": "ok", "service": "openwikirag-worker"}
        if path == b"/readyz":
            if not self._ready:
                return 503, {"status": "not_ready", "service": "openwikirag-worker"}
            return 200, {
                "status": "ready",
                "service": "openwikirag-worker",
                "checks": {"initialization": "ok"},
            }
        return 404, {"status": "not_found"}
