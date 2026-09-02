"""FastAPI dependencies that compose authentication with infrastructure."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.storage import LocalObjectStorage, ObjectStorage
from openwikirag.security.authentication import InvalidAccessTokenError, JWTAuthenticator
from openwikirag.security.authorization import Principal

settings = get_settings()
database_engine = create_database_engine(settings.database_url)
session_factory = create_session_factory(database_engine)
bearer_scheme = HTTPBearer(auto_error=False)
authenticator = JWTAuthenticator(
    secret=settings.jwt_secret,
    issuer=settings.jwt_issuer,
    audience=settings.jwt_audience,
)
object_storage = LocalObjectStorage(Path(settings.object_store_root))


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
        claims = authenticator.verify(credentials.credentials)
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
