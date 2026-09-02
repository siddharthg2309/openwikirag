"""Verification of signed access tokens at the application boundary."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import jwt
from jwt.exceptions import PyJWTError


class AuthenticationError(Exception):
    """Base error for invalid or unusable access tokens."""


class InvalidAccessTokenError(AuthenticationError):
    """Raised when an access token cannot be trusted or parsed."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Claims needed to locate the current user and tenant membership."""

    subject: str
    tenant_id: UUID
    expires_at: datetime


class AccessTokenProvider(Protocol):
    """Provider seam that an OIDC/JWKS adapter can implement later."""

    def verify(self, encoded_token: str) -> AccessTokenClaims:
        """Verify an access token and return its trusted identity claims."""


class JWTAuthenticator:
    """Verify local-development HS256 access tokens.

    The token identifies the provider subject and the requested tenant. It is
    deliberately not treated as the source of truth for the user's role.
    """

    _algorithm = "HS256"

    def __init__(self, *, secret: str, issuer: str, audience: str) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def verify(self, encoded_token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                encoded_token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "tenant_id", "iss", "aud", "iat", "exp"],
                },
            )
            subject = str(payload["sub"])
            tenant_id = UUID(str(payload["tenant_id"]))
            expires_at = datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
        except (PyJWTError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise InvalidAccessTokenError("The access token is invalid.") from exc

        if not subject:
            raise InvalidAccessTokenError("The access token subject is empty.")

        return AccessTokenClaims(
            subject=subject,
            tenant_id=tenant_id,
            expires_at=expires_at,
        )

    def issue_access_token(
        self,
        *,
        subject: str,
        tenant_id: UUID,
        ttl_seconds: int,
    ) -> str:
        """Issue a short-lived local access token for a known identity."""

        now = datetime.now(UTC)
        return str(
            jwt.encode(
                {
                    "sub": subject,
                    "tenant_id": str(tenant_id),
                    "iss": self._issuer,
                    "aud": self._audience,
                    "iat": now,
                    "exp": now + timedelta(seconds=ttl_seconds),
                    "typ": "access",
                },
                self._secret,
                algorithm=self._algorithm,
            )
        )


def generate_refresh_token() -> tuple[str, str]:
    """Return a raw refresh token and its non-reversible database hash."""

    raw_token = secrets.token_urlsafe(48)
    return raw_token, hash_refresh_token(raw_token)


def hash_refresh_token(raw_token: str) -> str:
    """Hash an opaque refresh token for indexed lookup and revocation."""

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
