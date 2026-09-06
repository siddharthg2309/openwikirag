"""OIDC/JWKS authentication and production configuration boundaries."""

import json
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app import auth_routes, dependencies
from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from openwikirag.core.config import Settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import Base
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authentication import (
    InvalidAccessTokenError,
    OIDCAuthenticator,
)
from openwikirag.security.authorization import Role


@dataclass(frozen=True, slots=True)
class OIDCContext:
    session_factory: Any
    user_id: UUID
    subject: str
    tenant_id: UUID


class _JWKSHandler(BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


_DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def oidc_context(tmp_path: Path) -> AsyncIterator[OIDCContext]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'oidc.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    subject = f"oidc|{uuid4()}"
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("OIDC test tenant")
        user = await repository.create_user(
            f"oidc-{uuid4()}@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.EDITOR)
        await session.commit()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        yield OIDCContext(session_factory, user.id, subject, tenant.id)
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.fixture
def key_pair() -> tuple[Any, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def jwks_url(key_pair: tuple[Any, Any]) -> Iterator[str]:
    _, public_key = key_pair
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    _JWKSHandler.body = json.dumps({"keys": [jwk]}).encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JWKSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/jwks"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _signed_token(
    private_key: Any,
    *,
    subject: str = "oidc|subject",
tenant_id: UUID | None = _DEFAULT_TENANT_ID,
    issuer: str = "https://issuer.example",
    audience: str = "openwikirag-api",
    expires_at: datetime | None = None,
    headers: dict[str, str] | None = None,
    extra_claims: dict[str, object] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": expires_at or now + timedelta(minutes=5),
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    payload.update(extra_claims or {})
    return str(
        jwt.encode(
            payload,
            private_key,
            algorithm="RS256",
            headers=headers if headers is not None else {"kid": "test-key"},
        )
    )


def _authenticator(jwks_url: str) -> OIDCAuthenticator:
    return OIDCAuthenticator(
        jwks_url=jwks_url,
        issuer="https://issuer.example",
        audience="openwikirag-api",
        jwks_timeout_seconds=2,
    )


def test_oidc_settings_require_production_verification() -> None:
    assert Settings(environment="development").auth_mode == "local"
    with pytest.raises(ValueError, match="requires OIDC mode"):
        Settings(environment="production")
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            environment="production",
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="openwikirag-api",
            oidc_jwks_url="http://issuer.example/jwks",
        )
    with pytest.raises(ValueError, match="asymmetric"):
        Settings(
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="openwikirag-api",
            oidc_jwks_url="http://127.0.0.1:8001/jwks",
            oidc_algorithms="HS256",
        )


def test_oidc_verifier_accepts_signed_token_from_local_jwks(
    key_pair: tuple[Any, Any], jwks_url: str
) -> None:
    private_key, _ = key_pair
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    claims = _authenticator(jwks_url).verify(
        _signed_token(private_key, subject="oidc|alice", tenant_id=tenant_id)
    )
    assert claims.subject == "oidc|alice"
    assert claims.tenant_id == tenant_id


@pytest.mark.parametrize(
    "case",
    [
        "issuer",
        "audience",
        "tenant",
        "missing-tenant",
        "kid",
        "unknown-kid",
    ],
)
def test_oidc_verifier_rejects_invalid_claims_or_key_id(
    key_pair: tuple[Any, Any],
    jwks_url: str,
    case: str,
) -> None:
    private_key, _ = key_pair
    if case == "issuer":
        token = _signed_token(private_key, issuer="https://other.example")
    elif case == "audience":
        token = _signed_token(private_key, audience="other-api")
    elif case == "tenant":
        token = _signed_token(
            private_key,
            tenant_id=None,
            extra_claims={"tenant_id": "not-a-uuid"},
        )
    elif case == "missing-tenant":
        token = _signed_token(private_key, tenant_id=None)
    elif case == "unknown-kid":
        token = _signed_token(private_key, headers={"kid": "unknown-key"})
    else:
        token = _signed_token(private_key, headers={})
    with pytest.raises(InvalidAccessTokenError):
        _authenticator(jwks_url).verify(token)


def test_oidc_verifier_rejects_expiry_algorithm_confusion_and_bad_signature(
    key_pair: tuple[Any, Any], jwks_url: str
) -> None:
    private_key, _ = key_pair
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _authenticator(jwks_url)
    with pytest.raises(InvalidAccessTokenError):
        verifier.verify(
            _signed_token(private_key, expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    with pytest.raises(InvalidAccessTokenError):
        verifier.verify(
            str(
                jwt.encode(
                    {"sub": "oidc|alice"},
                    "shared-secret-that-must-not-be-used",
                    algorithm="HS256",
                    headers={"kid": "test-key"},
                )
            )
        )
    with pytest.raises(InvalidAccessTokenError):
        verifier.verify(_signed_token(other_private_key))


def test_oidc_verifier_fails_closed_when_jwks_provider_fails() -> None:
    class FailingJWKS:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            del token
            raise RuntimeError("provider unavailable")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OIDCAuthenticator(
        jwks_url="http://unused.example/jwks",
        issuer="https://issuer.example",
        audience="openwikirag-api",
        jwks_client=FailingJWKS(),
    )
    with pytest.raises(InvalidAccessTokenError):
        verifier.verify(_signed_token(private_key))


def test_build_authenticator_uses_oidc_configuration(jwks_url: str) -> None:
    configured = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="openwikirag-api",
        oidc_jwks_url=jwks_url,
    )
    assert isinstance(dependencies.build_authenticator(configured), OIDCAuthenticator)


async def test_oidc_token_resolves_current_database_membership(
    oidc_context: OIDCContext,
    key_pair: tuple[Any, Any],
    jwks_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, _ = key_pair
    monkeypatch.setattr(dependencies, "authenticator", _authenticator(jwks_url))
    token = _signed_token(
        private_key,
        subject=oidc_context.subject,
        tenant_id=oidc_context.tenant_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "subject_id": str(oidc_context.user_id),
        "tenant_id": str(oidc_context.tenant_id),
        "role": "editor",
    }


def test_local_auth_routes_are_disabled_in_oidc_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oidc_settings = Settings(
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="openwikirag-api",
        oidc_jwks_url="http://127.0.0.1:8001/jwks",
    )
    monkeypatch.setattr(auth_routes, "settings", oidc_settings)
    with pytest.raises(HTTPException, match="Local authentication is disabled"):
        auth_routes._ensure_local_auth_enabled()
