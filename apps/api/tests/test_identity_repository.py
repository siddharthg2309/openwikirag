from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import Base
from openwikirag.infrastructure.repositories.identity import (
    DuplicateMembershipError,
    DuplicateUserError,
    IdentityIntegrityError,
    IdentityRepository,
)
from openwikirag.security.authorization import Role


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    async with session_factory() as test_session:
        yield test_session

    await engine.dispose()


async def create_identity(session: AsyncSession) -> tuple[IdentityRepository, UUID, UUID]:
    repository = IdentityRepository(session)
    tenant = await repository.create_tenant("Acme Engineering")
    user = await repository.create_user(" Alice@Example.com ")
    await session.commit()
    return repository, tenant.id, user.id


async def test_membership_round_trip_returns_typed_role(session: AsyncSession) -> None:
    repository, tenant_id, user_id = await create_identity(session)

    await repository.add_membership(tenant_id, user_id, Role.EDITOR)
    await session.commit()

    assert await repository.get_role_for_tenant(user_id, tenant_id) is Role.EDITOR


async def test_duplicate_membership_is_rejected(session: AsyncSession) -> None:
    repository, tenant_id, user_id = await create_identity(session)
    await repository.add_membership(tenant_id, user_id, Role.VIEWER)
    await session.commit()

    with pytest.raises(DuplicateMembershipError):
        await repository.add_membership(tenant_id, user_id, Role.ADMIN)


async def test_duplicate_email_is_rejected_after_normalization(session: AsyncSession) -> None:
    repository = IdentityRepository(session)
    await repository.create_user("Alice@Example.com")
    await session.commit()

    with pytest.raises(DuplicateUserError):
        await repository.create_user(" alice@example.com ")


async def test_duplicate_provider_subject_is_rejected(session: AsyncSession) -> None:
    repository = IdentityRepository(session)
    await repository.create_user(
        "alice@example.com",
        auth_provider_subject="oidc|alice",
    )
    await session.commit()

    with pytest.raises(DuplicateUserError):
        await repository.create_user(
            "other@example.com",
            auth_provider_subject="oidc|alice",
        )


async def test_membership_lookup_is_tenant_scoped(session: AsyncSession) -> None:
    repository, tenant_id, user_id = await create_identity(session)
    other_tenant = await repository.create_tenant("Other Engineering")
    await repository.add_membership(tenant_id, user_id, Role.VIEWER)
    await session.commit()

    assert await repository.get_role_for_tenant(user_id, tenant_id) is Role.VIEWER
    assert await repository.get_role_for_tenant(user_id, other_tenant.id) is None


async def test_membership_with_unknown_tenant_is_an_integrity_error(
    session: AsyncSession,
) -> None:
    repository = IdentityRepository(session)
    user = await repository.create_user("user@example.com")
    await session.commit()

    with pytest.raises(IdentityIntegrityError):
        await repository.add_membership(UUID(int=0), user.id, Role.VIEWER)
