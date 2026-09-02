from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.security.authorization import Role

from ..models import Membership, RefreshToken, Tenant, User


class DuplicateIdentityError(Exception):
    """Base error for uniqueness conflicts in identity data."""


class DuplicateUserError(DuplicateIdentityError):
    """Raised when an email already identifies a user."""


class DuplicateMembershipError(DuplicateIdentityError):
    """Raised when a user already belongs to a tenant."""


class IdentityIntegrityError(Exception):
    """Raised when identity data violates a relational constraint."""


type IdentityContext = tuple[User, Role, str]


class IdentityRepository:
    """Persistence operations for tenants, users, and memberships.

    Methods flush but do not commit. The application service owns the
    transaction boundary so multiple repository calls can be atomic.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_tenant(self, name: str) -> Tenant:
        tenant = Tenant(name=name.strip())
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def create_user(
        self,
        email: str,
        *,
        auth_provider_subject: str | None = None,
        password_hash: str | None = None,
    ) -> User:
        normalized_email = email.strip().casefold()
        existing_user = await self._session.scalar(
            select(User.id).where(User.email == normalized_email)
        )
        if existing_user is not None:
            raise DuplicateUserError("A user with this email already exists.")

        if auth_provider_subject is not None:
            existing_provider_identity = await self._session.scalar(
                select(User.id).where(User.auth_provider_subject == auth_provider_subject)
            )
            if existing_provider_identity is not None:
                raise DuplicateUserError("This provider subject already identifies a user.")

        user = User(
            email=normalized_email,
            auth_provider_subject=auth_provider_subject,
            password_hash=password_hash,
        )
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise IdentityIntegrityError("The user violates an identity constraint.") from exc
        return user

    async def add_membership(
        self,
        tenant_id: UUID,
        user_id: UUID,
        role: Role,
    ) -> Membership:
        existing_membership = await self._session.scalar(
            select(Membership.id).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user_id,
            )
        )
        if existing_membership is not None:
            raise DuplicateMembershipError(
                "This user is already a member of the tenant."
            )

        membership = Membership(tenant_id=tenant_id, user_id=user_id, role=role.value)
        self._session.add(membership)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise IdentityIntegrityError("The membership violates an identity constraint.") from exc
        return membership

    async def get_role_for_tenant(self, user_id: UUID, tenant_id: UUID) -> Role | None:
        statement = select(Membership.role).where(
            Membership.user_id == user_id,
            Membership.tenant_id == tenant_id,
        )
        role_value = await self._session.scalar(statement)
        if role_value is None:
            return None
        return Role(role_value)

    async def get_identity_context(
        self,
        auth_provider_subject: str,
        tenant_id: UUID,
    ) -> IdentityContext | None:
        """Load the user, current role, and tenant status for a token subject."""

        statement = (
            select(User, Membership.role, Tenant.status)
            .join(Membership, Membership.user_id == User.id)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                User.auth_provider_subject == auth_provider_subject,
                Membership.tenant_id == tenant_id,
            )
        )
        result = await self._session.execute(statement)
        row = result.one_or_none()
        if row is None:
            return None

        user, role_value, tenant_status = row
        return user, Role(str(role_value)), str(tenant_status)

    async def get_user_by_email(self, email: str) -> User | None:
        """Load a local-auth user by the repository's normalized email form."""

        normalized_email = email.strip().casefold()
        return cast(
            User | None,
            await self._session.scalar(select(User).where(User.email == normalized_email)),
        )

    async def get_identity_context_for_user(
        self,
        user_id: UUID,
        tenant_id: UUID,
    ) -> IdentityContext | None:
        """Load current membership state for an internal user id."""

        statement = (
            select(User, Membership.role, Tenant.status)
            .join(Membership, Membership.user_id == User.id)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(User.id == user_id, Membership.tenant_id == tenant_id)
        )
        result = await self._session.execute(statement)
        row = result.one_or_none()
        if row is None:
            return None

        user, role_value, tenant_status = row
        return user, Role(str(role_value)), str(tenant_status)

    async def create_refresh_token(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=user_id,
            tenant_id=tenant_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        """Load a refresh-token record without exposing its raw secret."""

        return cast(
            RefreshToken | None,
            await self._session.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == token_hash)
            ),
        )

    async def revoke_refresh_token(
        self,
        token: RefreshToken,
        *,
        replaced_by_id: UUID | None = None,
    ) -> None:
        token.revoked_at = datetime.now(UTC)
        token.replaced_by_id = replaced_by_id
        await self._session.flush()
