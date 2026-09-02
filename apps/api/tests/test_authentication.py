from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import AuditEvent, Base, User
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authorization import Role


class AuthContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        user_id: UUID,
        subject: str,
        tenant_id: UUID,
    ) -> None:
        self.session_factory = session_factory
        self.user_id = user_id
        self.subject = subject
        self.tenant_id = tenant_id


@pytest.fixture
async def auth_context(tmp_path: Path) -> AsyncIterator[AuthContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'authentication.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    subject = "oidc|alice"
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("Acme Engineering")
        user = await repository.create_user(
            "alice@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.EDITOR)
        await session.commit()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as test_session:
            yield test_session

    app.dependency_overrides[get_session] = override_get_session
    yield AuthContext(session_factory, user.id, subject, tenant.id)
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


def make_token(
    subject: str,
    tenant_id: UUID,
    *,
    secret: str | None = None,
    expires_at: datetime | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "tenant_id": str(tenant_id),
        "iss": get_settings().jwt_issuer,
        "aud": get_settings().jwt_audience,
        "iat": now,
        "exp": expires_at or now + timedelta(minutes=5),
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(
        claims,
        secret or get_settings().jwt_secret,
        algorithm="HS256",
    )


async def get_me(token: str | None) -> Response:
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/me", headers=headers)


async def post_auth(payload: dict[str, Any]) -> Response:
    transport = ASGITransport(app=app)
    route = "register" if "tenant_name" in payload else "token"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/v1/auth/{route}", json=payload)


async def test_valid_token_uses_database_role_not_token_role(auth_context: AuthContext) -> None:
    token = make_token(
        auth_context.subject,
        auth_context.tenant_id,
        extra_claims={"role": "operator"},
    )

    response = await get_me(token)

    assert response.status_code == 200
    assert response.json() == {
        "subject_id": str(auth_context.user_id),
        "tenant_id": str(auth_context.tenant_id),
        "role": "editor",
    }


async def test_missing_bearer_token_is_unauthorized(auth_context: AuthContext) -> None:
    response = await get_me(None)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_invalid_signature_is_unauthorized(auth_context: AuthContext) -> None:
    token = make_token(
        auth_context.subject,
        auth_context.tenant_id,
        secret="a-different-development-secret-that-is-long-enough",
    )

    response = await get_me(token)

    assert response.status_code == 401


async def test_expired_token_is_unauthorized(auth_context: AuthContext) -> None:
    token = make_token(
        auth_context.subject,
        auth_context.tenant_id,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    response = await get_me(token)

    assert response.status_code == 401


async def test_missing_membership_is_forbidden(auth_context: AuthContext) -> None:
    token = make_token("oidc|unknown", auth_context.tenant_id)

    response = await get_me(token)

    assert response.status_code == 403


async def test_inactive_user_is_forbidden(auth_context: AuthContext) -> None:
    async with auth_context.session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == auth_context.user_id)
            .values(is_active=False)
        )
        await session.commit()

    token = make_token(auth_context.subject, auth_context.tenant_id)
    response = await get_me(token)

    assert response.status_code == 403


async def test_local_registration_hashes_password_and_writes_audit(
    auth_context: AuthContext,
) -> None:
    response = await post_auth(
        {
            "tenant_name": "Local Auth Tenant",
            "email": "local@example.com",
            "password": "a-strong-local-password",
        }
    )

    assert response.status_code == 201
    async with auth_context.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "local@example.com"))
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.register")
        )

    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash != "a-strong-local-password"
    assert audit is not None


async def test_password_login_and_refresh_rotation(auth_context: AuthContext) -> None:
    registration = await post_auth(
        {
            "tenant_name": "Token Tenant",
            "email": "token@example.com",
            "password": "another-strong-password",
        }
    )
    tenant_id = registration.json()["tenant_id"]
    login = await post_auth(
        {
            "grant_type": "password",
            "email": "token@example.com",
            "password": "another-strong-password",
            "tenant_id": tenant_id,
        }
    )

    assert login.status_code == 200
    first_tokens = login.json()
    rotated = await post_auth(
        {
            "grant_type": "refresh_token",
            "refresh_token": first_tokens["refresh_token"],
        }
    )

    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != first_tokens["refresh_token"]
    replay = await post_auth(
        {
            "grant_type": "refresh_token",
            "refresh_token": first_tokens["refresh_token"],
        }
    )
    assert replay.status_code == 401

    protected = await get_me(rotated.json()["access_token"])
    assert protected.status_code == 200
    assert protected.json()["role"] == "admin"


async def test_invalid_local_password_is_unauthorized(auth_context: AuthContext) -> None:
    registration = await post_auth(
        {
            "tenant_name": "Credential Tenant",
            "email": "credentials@example.com",
            "password": "correct-strong-password",
        }
    )
    response = await post_auth(
        {
            "grant_type": "password",
            "email": "credentials@example.com",
            "password": "incorrect-password",
            "tenant_id": registration.json()["tenant_id"],
        }
    )

    assert response.status_code == 401
