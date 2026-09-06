"""Local development authentication routes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.auth import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    LocalAuthService,
)
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.repositories.identity import DuplicateIdentityError

from .dependencies import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)


class RegisterResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    email: str


class TokenRequest(BaseModel):
    grant_type: Literal["password", "refresh_token"]
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    tenant_id: UUID | None = None
    refresh_token: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_grant_fields(self) -> "TokenRequest":
        if self.grant_type == "password":
            if self.email is None or self.password is None or self.tenant_id is None:
                raise ValueError("Password grant requires email, password, and tenant_id.")
        elif self.refresh_token is None:
            raise ValueError("Refresh-token grant requires refresh_token.")
        return self


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


def _ensure_local_auth_enabled() -> None:
    if settings.environment != "development" or settings.auth_mode != "local":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Local authentication is disabled outside development.",
        )


def _service(session: Annotated[AsyncSession, Depends(get_session)]) -> LocalAuthService:
    return LocalAuthService(session, settings)


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    service: Annotated[LocalAuthService, Depends(_service)],
) -> RegisterResponse:
    """Create a local-development tenant and its initial administrator."""

    _ensure_local_auth_enabled()
    try:
        result = await service.register(
            tenant_name=request.tenant_name,
            email=request.email,
            password=request.password,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except DuplicateIdentityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return RegisterResponse(
        user_id=result.user_id,
        tenant_id=result.tenant_id,
        email=result.email,
    )


@router.post("/token", response_model=TokenResponse)
async def token(
    request: TokenRequest,
    service: Annotated[LocalAuthService, Depends(_service)],
) -> TokenResponse:
    """Issue an access/refresh pair or rotate a presented refresh token."""

    _ensure_local_auth_enabled()
    try:
        if request.grant_type == "password":
            assert request.email is not None
            assert request.password is not None
            assert request.tenant_id is not None
            result = await service.password_token(
                email=request.email,
                password=request.password,
                tenant_id=request.tenant_id,
            )
        else:
            assert request.refresh_token is not None
            result = await service.refresh_token(request.refresh_token)
    except (InvalidCredentialsError, InvalidRefreshTokenError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )
