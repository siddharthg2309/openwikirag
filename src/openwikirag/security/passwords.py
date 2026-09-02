"""Password hashing boundary for local development authentication."""

from pwdlib import PasswordHash


class PasswordService:
    """Hash and verify passwords without exposing the hashing library upstream."""

    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._hasher.verify(password, password_hash)
