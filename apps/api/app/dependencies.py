"""FastAPI dependencies that compose authentication with infrastructure."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.config import Settings, get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.storage import ObjectStorage, build_object_storage
from openwikirag.security.authentication import (
    AccessTokenProvider,
    InvalidAccessTokenError,
    JWTAuthenticator,
    OIDCAuthenticator,
)
from openwikirag.security.authorization import Principal

settings = get_settings()
database_engine = create_database_engine(settings.database_url)
session_factory = create_session_factory(database_engine)
bearer_scheme = HTTPBearer(auto_error=False)


def build_authenticator(current_settings: Settings = settings) -> AccessTokenProvider:
    """Build the configured token verifier once at process startup."""

    if current_settings.auth_mode == "oidc":
        return OIDCAuthenticator(
            jwks_url=current_settings.oidc_jwks_url,
            issuer=current_settings.oidc_issuer,
            audience=current_settings.oidc_audience,
            algorithms=current_settings.oidc_algorithm_list,
            tenant_claim=current_settings.oidc_tenant_claim,
            jwks_cache_seconds=current_settings.oidc_jwks_cache_seconds,
            jwks_timeout_seconds=current_settings.oidc_jwks_timeout_seconds,
        )
    return JWTAuthenticator(
        secret=current_settings.jwt_secret,
        issuer=current_settings.jwt_issuer,
        audience=current_settings.jwt_audience,
    )


authenticator = build_authenticator()
object_storage = build_object_storage(settings)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Open one database session per request and close it after the request."""

    async with session_factory() as session:
        yield session


def get_object_storage() -> ObjectStorage:
    """Return the process storage adapter; production can replace this dependency."""

    return object_storage


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    """Verify a token and resolve its current tenant membership."""

    if credentials is None:
        raise _unauthorized("Bearer authentication is required.")

    try:
        claims = await asyncio.to_thread(authenticator.verify, credentials.credentials)
    except InvalidAccessTokenError as exc:
        raise _unauthorized("The access token is invalid.") from exc

    await set_tenant_context(session, claims.tenant_id)
    context = await IdentityRepository(session).get_identity_context(
        claims.subject,
        claims.tenant_id,
    )
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity has no membership in this tenant.",
        )

    user, role, tenant_status = context
    if not user.is_active or tenant_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity or tenant is inactive.",
        )

    return Principal(
        subject_id=str(user.id),
        tenant_id=str(claims.tenant_id),
        role=role,
    )
