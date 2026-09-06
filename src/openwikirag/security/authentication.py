"""Verification of signed access tokens at the application boundary."""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError


class AuthenticationError(Exception):
    """Base error for invalid or unusable access tokens."""


class InvalidAccessTokenError(AuthenticationError):
    """Raised when an access token cannot be trusted or parsed."""


_OIDC_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Claims needed to locate the current user and tenant membership."""

    subject: str
    tenant_id: UUID
    expires_at: datetime


class AccessTokenProvider(Protocol):
    """Provider seam shared by local and external token verifiers."""

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


class OIDCAuthenticator:
    """Verify asymmetric OIDC access tokens against a rotating JWKS set."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        algorithms: Sequence[str] = ("RS256",),
        tenant_claim: str = "tenant_id",
        jwks_cache_seconds: int = 300,
        jwks_timeout_seconds: float = 5.0,
        jwks_client: Any | None = None,
    ) -> None:
        normalized_algorithms = tuple(
            dict.fromkeys(item.strip() for item in algorithms if item.strip())
        )
        if not normalized_algorithms or any(
            item not in _OIDC_ASYMMETRIC_ALGORITHMS for item in normalized_algorithms
        ):
            raise ValueError("OIDC requires at least one asymmetric JWT algorithm.")
        if not issuer.strip() or not audience.strip():
            raise ValueError("OIDC issuer and audience cannot be empty.")
        if not tenant_claim.strip():
            raise ValueError("OIDC tenant claim cannot be empty.")
        if jwks_cache_seconds < 1 or jwks_timeout_seconds <= 0:
            raise ValueError("OIDC JWKS cache and timeout must be positive.")
        self._issuer = issuer
        self._audience = audience
        self._algorithms = normalized_algorithms
        self._tenant_claim = tenant_claim
        self._jwks = jwks_client or PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            cache_keys=True,
            max_cached_keys=16,
            lifespan=jwks_cache_seconds,
            timeout=jwks_timeout_seconds,
        )

    def verify(self, encoded_token: str) -> AccessTokenClaims:
        """Verify a signed OIDC token; all key/provider failures fail closed."""

        try:
            header = jwt.get_unverified_header(encoded_token)
            if not isinstance(header.get("kid"), str) or not header["kid"]:
                raise InvalidAccessTokenError("The access token has no signing key id.")
            if header.get("alg") not in self._algorithms:
                raise InvalidAccessTokenError("The access token algorithm is not allowed.")
            signing_key = self._jwks.get_signing_key_from_jwt(encoded_token)
            payload = jwt.decode(
                encoded_token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "iss", "aud", "iat", "exp", self._tenant_claim],
                },
            )
            subject = payload["sub"]
            tenant_value = payload[self._tenant_claim]
            tenant_id = UUID(str(tenant_value))
            expires_at = datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
        except InvalidAccessTokenError:
            raise
        except Exception as exc:
            raise InvalidAccessTokenError("The access token is invalid or unavailable.") from exc

        if not isinstance(subject, str) or not subject:
            raise InvalidAccessTokenError("The access token subject is empty.")

        return AccessTokenClaims(subject=subject, tenant_id=tenant_id, expires_at=expires_at)


def generate_refresh_token() -> tuple[str, str]:
    """Return a raw refresh token and its non-reversible database hash."""

    raw_token = secrets.token_urlsafe(48)
    return raw_token, hash_refresh_token(raw_token)


def hash_refresh_token(raw_token: str) -> str:
    """Hash an opaque refresh token for indexed lookup and revocation."""

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
