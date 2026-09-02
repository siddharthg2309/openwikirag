"""Integration proof for the migration and PostgreSQL tenant policies."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.models import AuditEvent, Membership
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authentication import JWTAuthenticator
from openwikirag.security.authorization import Role


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = os.environ.get("OPENWIKIRAG_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set OPENWIKIRAG_TEST_POSTGRES_URL to run PostgreSQL integration tests.")

    project_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["OPENWIKIRAG_DATABASE_URL"] = url
    environment["OPENWIKIRAG_MIGRATION_DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=environment,
        check=True,
    )
    return url


@pytest.fixture
async def postgres_session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(postgres_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'openwikirag_rls_test'
                    ) THEN
                        CREATE ROLE openwikirag_rls_test NOSUPERUSER NOLOGIN;
                    END IF;
                END
                $$
                """
            )
        )
        await session.execute(text("GRANT USAGE ON SCHEMA public TO openwikirag_rls_test"))
        await session.execute(
            text(
                "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                "TO openwikirag_rls_test"
            )
        )
        await session.commit()
        await session.execute(text("SET ROLE openwikirag_rls_test"))
        yield session
    await engine.dispose()


async def test_postgres_rls_filters_memberships_and_audits_by_transaction_tenant(
    postgres_session: AsyncSession,
) -> None:
    repository = IdentityRepository(postgres_session)
    audit = AuditRepository(postgres_session)
    suffix = uuid4().hex
    tenant_a = await repository.create_tenant(f"RLS A {suffix}")
    tenant_b = await repository.create_tenant(f"RLS B {suffix}")
    user = await repository.create_user(
        f"rls-{suffix}@example.com",
        auth_provider_subject=f"oidc|rls-{suffix}",
    )

    await set_tenant_context(postgres_session, tenant_a.id)
    await repository.add_membership(tenant_a.id, user.id, Role.VIEWER)
    await audit.record(
        action="rls.test.a",
        resource_type="tenant",
        tenant_id=tenant_a.id,
        outcome="success",
    )
    await postgres_session.commit()

    await set_tenant_context(postgres_session, tenant_b.id)
    await repository.add_membership(tenant_b.id, user.id, Role.EDITOR)
    await audit.record(
        action="rls.test.b",
        resource_type="tenant",
        tenant_id=tenant_b.id,
        outcome="success",
    )
    await postgres_session.commit()

    await set_tenant_context(postgres_session, tenant_a.id)
    token = JWTAuthenticator(
        secret=get_settings().jwt_secret,
        issuer=get_settings().jwt_issuer,
        audience=get_settings().jwt_audience,
    ).issue_access_token(
        subject=user.auth_provider_subject or "",
        tenant_id=tenant_a.id,
        ttl_seconds=900,
    )

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield postgres_session

    app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"

    await set_tenant_context(postgres_session, tenant_a.id)
    visible_memberships_a = await postgres_session.scalars(
        select(Membership).where(Membership.user_id == user.id)
    )
    visible_audits_a = await postgres_session.scalars(
        select(AuditEvent).where(AuditEvent.action.like("rls.test.%"))
    )
    assert {membership.tenant_id for membership in visible_memberships_a} == {tenant_a.id}
    assert {event.tenant_id for event in visible_audits_a} == {tenant_a.id}
    assert await repository.get_role_for_tenant(user.id, tenant_b.id) is None

    await set_tenant_context(postgres_session, tenant_b.id)
    visible_memberships_b = await postgres_session.scalars(
        select(Membership).where(Membership.user_id == user.id)
    )
    visible_audits_b = await postgres_session.scalars(
        select(AuditEvent).where(AuditEvent.action.like("rls.test.%"))
    )
    assert {membership.tenant_id for membership in visible_memberships_b} == {tenant_b.id}
    assert {event.tenant_id for event in visible_audits_b} == {tenant_b.id}
    assert await repository.get_role_for_tenant(user.id, tenant_b.id) is Role.EDITOR
