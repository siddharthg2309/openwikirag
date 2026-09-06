"""Drive the live non-voice document path through the Compose API."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, cast
from uuid import UUID, uuid4

import httpx

SOURCE = (
    b"# Live document demo\n"
    b"OpenWikiRAG keeps immutable source provenance across the live pipeline.\n"
    b"Canonical evidence is read before a search result is released.\n"
)
QUERY = "immutable source provenance"


class DemoError(Exception):
    """Raised when the live document walkthrough cannot prove its contract."""


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise DemoError(f"{endpoint} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DemoError(f"{endpoint} returned a non-object JSON value")
    return cast(dict[str, Any], value)


def _require_status(response: httpx.Response, expected: int, endpoint: str) -> None:
    if response.status_code != expected:
        raise DemoError(f"{endpoint} returned HTTP {response.status_code}")


def _required_uuid(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise DemoError(f"response is missing {field}")
    try:
        UUID(value)
    except ValueError as exc:
        raise DemoError(f"response contains an invalid {field}") from exc
    return value


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be numeric") from exc
    if not 10 <= parsed <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 10 and 300 seconds")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the live OpenWikiRAG document demo.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:28080",
        help="API base URL exposed by the isolated Compose project.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_timeout,
        default=180.0,
        help="Maximum time to wait for the durable ingestion job.",
    )
    return parser


async def _run(*, base_url: str, timeout_seconds: float) -> None:
    email = f"live-demo-{uuid4().hex}@example.com"
    password = "OpenWikiRAG-live-demo-password"
    request_headers = {"X-Request-ID": "live-non-voice-demo"}

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        register = await client.post(
            "/api/v1/auth/register",
            headers=request_headers,
            json={
                "tenant_name": "OpenWikiRAG Live Demo",
                "email": email,
                "password": password,
            },
        )
        _require_status(register, 201, "register")
        registration = _json_object(register, "register")
        tenant_id = _required_uuid(registration, "tenant_id")

        token = await client.post(
            "/api/v1/auth/token",
            headers=request_headers,
            json={
                "grant_type": "password",
                "email": email,
                "password": password,
                "tenant_id": tenant_id,
            },
        )
        _require_status(token, 200, "token")
        token_payload = _json_object(token, "token")
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise DemoError("token response is missing an access token")
        auth_headers = {
            **request_headers,
            "Authorization": f"Bearer {access_token}",
        }

        upload = await client.post(
            "/api/v1/documents",
            headers=auth_headers,
            files={"upload": ("live-demo.md", SOURCE, "text/markdown")},
        )
        _require_status(upload, 202, "document upload")
        uploaded = _json_object(upload, "document upload")
        document_id = _required_uuid(uploaded, "document_id")
        document_version_id = _required_uuid(uploaded, "document_version_id")
        job_id = _required_uuid(uploaded, "ingestion_job_id")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        job_status = "pending"
        while loop.time() < deadline:
            progress_response = await client.get(
                f"/api/v1/jobs/{job_id}",
                headers=auth_headers,
            )
            _require_status(progress_response, 200, "job progress")
            progress = _json_object(progress_response, "job progress")
            job_status = progress.get("status", "unknown")
            if job_status == "succeeded":
                break
            if job_status == "dead_letter":
                raise DemoError("ingestion job reached dead_letter")
            await asyncio.sleep(0.5)
        else:
            raise DemoError(f"ingestion job timed out in status {job_status}")

        pages_response = await client.get(
            "/api/v1/wiki/pages",
            headers=auth_headers,
            params={"limit": 100},
        )
        _require_status(pages_response, 200, "WikiRAG page listing")
        pages = _json_object(pages_response, "WikiRAG page listing")
        page_items = pages.get("items")
        if not isinstance(page_items, list):
            raise DemoError("WikiRAG page listing has no item collection")
        matching_pages = [
            item
            for item in page_items
            if isinstance(item, dict)
            and item.get("document_version_id") == document_version_id
        ]
        if not matching_pages:
            raise DemoError("WikiRAG page listing has no matching document version")

        search_response = await client.post(
            "/api/v1/search",
            headers=auth_headers,
            json={"query": QUERY, "mode": "sparse", "limit": 3},
        )
        _require_status(search_response, 200, "search")
        search = _json_object(search_response, "search")
        hits = search.get("hits")
        if not isinstance(hits, list) or not hits:
            raise DemoError("search returned no hits")
        first_hit = hits[0]
        if not isinstance(first_hit, dict):
            raise DemoError("search returned an invalid hit")
        evidence = first_hit.get("evidence")
        if not isinstance(evidence, dict):
            raise DemoError("search hit has no evidence")
        payload = evidence.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("document_version_id") != document_version_id
        ):
            raise DemoError("search hit does not belong to the uploaded version")
        chunk = evidence.get("chunk")
        if not isinstance(chunk, dict) or QUERY not in str(chunk.get("text", "")):
            raise DemoError("search hit is not canonical evidence for the demo query")

    print(
        "non_voice_live_demo_passed "
        f"document_id={document_id} "
        f"document_version_id={document_version_id} "
        f"job_id={job_id} "
        f"source_bytes={len(SOURCE)} "
        f"page_count={len(matching_pages)} "
        f"search_hits={len(hits)} "
        "job_status=succeeded storage=filesystem "
        "providers=postgres,redis,qdrant"
    )


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(base_url=args.base_url, timeout_seconds=args.timeout_seconds))
    except (DemoError, httpx.HTTPError, TimeoutError):
        print("non_voice_live_demo_failed reason=workflow_or_dependency_failure", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
