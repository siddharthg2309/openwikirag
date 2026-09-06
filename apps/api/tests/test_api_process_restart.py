"""Opt-in proof that the API recovers private conversation rows after restart."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_postgres_integration import postgres_session as postgres_session
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from openwikirag.application.conversations import ConversationService
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import set_tenant_context
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authentication import JWTAuthenticator
from openwikirag.security.authorization import Principal, Role


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_api(project_root: Path, database_url: str, port: int) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "OPENWIKIRAG_DATABASE_URL": database_url,
            "OPENWIKIRAG_MIGRATION_DATABASE_URL": database_url,
            "OPENWIKIRAG_API_HOST": "127.0.0.1",
            "OPENWIKIRAG_API_PORT": str(port),
            "OPENWIKIRAG_GRAPH_ENABLED": "false",
            "OPENWIKIRAG_SCORE_CACHE_ENABLED": "false",
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=project_root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_api(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def _get_conversation(port: int, token: str, conversation_id: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=url, timeout=1.0) as client:
        for _ in range(60):
            try:
                health = await client.get("/healthz")
                if health.status_code != 200:
                    await asyncio.sleep(0.1)
                    continue
                response = await client.get(
                    f"/api/v1/conversations/{conversation_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
                continue
            if response.status_code != 200:
                detail = response.text
                raise AssertionError(f"conversation read failed: {response.status_code} {detail}")
            return response.json()
    raise AssertionError("API process did not become ready within six seconds")


@pytest.mark.asyncio
async def test_conversation_survives_separate_api_process_restart(
    postgres_url: str, postgres_session: AsyncSession
) -> None:
    if os.environ.get("OPENWIKIRAG_TEST_API_RESTART") != "1":
        pytest.skip("Set OPENWIKIRAG_TEST_API_RESTART=1 to launch API subprocesses.")

    repository = IdentityRepository(postgres_session)
    suffix = uuid4().hex
    tenant = await repository.create_tenant(f"api-restart-{suffix}")
    owner = await repository.create_user(
        f"api-restart-{suffix}@example.com",
        auth_provider_subject=f"api-restart|{suffix}",
    )
    await set_tenant_context(postgres_session, tenant.id)
    await repository.add_membership(tenant.id, owner.id, Role.VIEWER)
    await postgres_session.commit()

    principal = Principal(str(owner.id), str(tenant.id), Role.VIEWER)
    conversation = await ConversationService(postgres_session, principal).create("Process durable")
    await ConversationService(postgres_session, principal).append(
        conversation.id, "user", {"text": "survive restart"}
    )
    await postgres_session.commit()
    token = JWTAuthenticator(
        secret=get_settings().jwt_secret,
        issuer=get_settings().jwt_issuer,
        audience=get_settings().jwt_audience,
    ).issue_access_token(
        subject=owner.auth_provider_subject or "",
        tenant_id=tenant.id,
        ttl_seconds=900,
    )

    project_root = Path(__file__).parents[3]
    port = _free_port()
    first = _start_api(project_root, postgres_url, port)
    second: subprocess.Popen[bytes] | None = None
    try:
        first_payload = await _get_conversation(port, token, str(conversation.id))
        assert first_payload["id"] == str(conversation.id)
        assert first_payload["title"] == "Process durable"
        assert [(item["sequence"], item["content"]) for item in first_payload["messages"]] == [
            (1, {"text": "survive restart"})
        ]
        _stop_api(first)

        second = _start_api(project_root, postgres_url, port)
        second_payload = await _get_conversation(port, token, str(conversation.id))
        assert second_payload == first_payload
    finally:
        _stop_api(first)
        if second is not None:
            _stop_api(second)
