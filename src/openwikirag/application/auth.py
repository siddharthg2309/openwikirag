"""Local authentication use cases and token lifecycle orchestration."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.config import Settings
from openwikirag.infrastructure.database import set_tenant_context
from openwikirag.infrastructure.models import User
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.repositories.identity import (
    IdentityRepository,
)
from openwikirag.security.authentication import (
    JWTAuthenticator,
    generate_refresh_token,
    hash_refresh_token,
)
from openwikirag.security.authorization import Role
from openwikirag.security.passwords import PasswordService


class LocalAuthError(Exception):
    """Base error for local authentication use-case failures."""


class InvalidCredentialsError(LocalAuthError):
    """Raised without revealing whether email or password was incorrect."""


class InvalidRefreshTokenError(LocalAuthError):
    """Raised for missing, expired, revoked, or no-longer-authorized tokens."""


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user_id: UUID
    tenant_id: UUID
    email: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


class LocalAuthService:
    """Coordinate local credentials, membership state, tokens, and audit rows."""

    _minimum_password_length = 12

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._identity = IdentityRepository(session)
        self._audit = AuditRepository(session)
        self._passwords = PasswordService()
        self._tokens = JWTAuthenticator(
            secret=settings.jwt_secret,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )

    async def register(
        self,
        *,
        tenant_name: str,
        email: str,
        password: str,
    ) -> RegistrationResult:
        self._validate_password(password)
        provider_subject = f"local:{secrets.token_urlsafe(24)}"

        try:
            tenant = await self._identity.create_tenant(tenant_name)
            await set_tenant_context(self._session, tenant.id)
            user = await self._identity.create_user(
                email,
                auth_provider_subject=provider_subject,
                password_hash=self._passwords.hash(password),
            )
            await self._identity.add_membership(tenant.id, user.id, Role.ADMIN)
            await self._audit.record(
                action="auth.register",
                resource_type="tenant",
                resource_id=str(tenant.id),
                tenant_id=tenant.id,
                actor_user_id=user.id,
                outcome="success",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        return RegistrationResult(user_id=user.id, tenant_id=tenant.id, email=user.email)

    async def password_token(
        self,
        *,
        email: str,
        password: str,
        tenant_id: UUID,
    ) -> TokenPair:
        user = await self._identity.get_user_by_email(email)
        if user is None or user.password_hash is None:
            raise InvalidCredentialsError("Invalid email or password.")
        if not self._passwords.verify(password, user.password_hash):
            raise InvalidCredentialsError("Invalid email or password.")

        await set_tenant_context(self._session, tenant_id)
        context = await self._identity.get_identity_context_for_user(user.id, tenant_id)
        if context is None:
            raise InvalidCredentialsError("The user is not a member of this tenant.")

        current_user, _, tenant_status = context
        if not current_user.is_active or tenant_status != "active":
            raise InvalidCredentialsError("The identity or tenant is inactive.")

        try:
            token_pair = self._issue_token_pair(current_user, tenant_id)
            await self._identity.create_refresh_token(
                user_id=current_user.id,
                tenant_id=tenant_id,
                token_hash=hash_refresh_token(token_pair.refresh_token),
                expires_at=datetime.now(UTC)
                + timedelta(days=self._settings.refresh_token_ttl_days),
            )
            await self._audit.record(
                action="auth.login",
                resource_type="user",
                resource_id=str(current_user.id),
                tenant_id=tenant_id,
                actor_user_id=current_user.id,
                outcome="success",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return token_pair

    async def refresh_token(self, raw_refresh_token: str) -> TokenPair:
        token = await self._identity.get_refresh_token(hash_refresh_token(raw_refresh_token))
        now = datetime.now(UTC)
        token_expires_at = (
            token.expires_at.replace(tzinfo=UTC)
            if token is not None and token.expires_at.tzinfo is None
            else token.expires_at
            if token is not None
            else None
        )
        if (
            token is None
            or token.revoked_at is not None
            or token_expires_at is None
            or token_expires_at <= now
        ):
            raise InvalidRefreshTokenError("The refresh token is invalid.")

        await set_tenant_context(self._session, token.tenant_id)
        context = await self._identity.get_identity_context_for_user(
            token.user_id,
            token.tenant_id,
        )
        if context is None:
            raise InvalidRefreshTokenError("The refresh token is no longer authorized.")

        user, _, tenant_status = context
        if not user.is_active or tenant_status != "active":
            raise InvalidRefreshTokenError("The refresh token is no longer authorized.")

        try:
            next_pair = self._issue_token_pair(user, token.tenant_id)
            next_hash = hash_refresh_token(next_pair.refresh_token)
            replacement = await self._identity.create_refresh_token(
                user_id=user.id,
                tenant_id=token.tenant_id,
                token_hash=next_hash,
                expires_at=now + timedelta(days=self._settings.refresh_token_ttl_days),
            )
            await self._identity.revoke_refresh_token(
                token,
                replaced_by_id=replacement.id,
            )
            await self._audit.record(
                action="auth.refresh",
                resource_type="refresh_token",
                resource_id=str(token.id),
                tenant_id=token.tenant_id,
                actor_user_id=user.id,
                outcome="success",
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return next_pair

    def _issue_token_pair(self, user: User, tenant_id: UUID) -> TokenPair:
        if user.auth_provider_subject is None:
            raise InvalidCredentialsError("The identity cannot issue a token.")

        access_token = self._tokens.issue_access_token(
            subject=user.auth_provider_subject,
            tenant_id=tenant_id,
            ttl_seconds=self._settings.access_token_ttl_seconds,
        )
        refresh_token, _ = generate_refresh_token()
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_seconds,
        )

    def _validate_password(self, password: str) -> None:
        if len(password) < self._minimum_password_length:
            raise ValueError(
                f"Password must contain at least {self._minimum_password_length} characters."
            )
