"""Replaceable object-storage ports and the deterministic local adapter."""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Protocol


class ObjectStorageError(Exception):
    """Raised when an object cannot be written, read, or deleted."""


class ObjectStorage(Protocol):
    """Minimal storage contract used by document application services."""

    async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
        """Store one immutable object, replacing only the same generated key."""

    async def get(self, *, object_key: str) -> bytes:
        """Read one object by its application-generated key."""

    async def delete(self, *, object_key: str) -> None:
        """Delete an object during compensating cleanup."""


class LocalObjectStorage:
    """Filesystem-backed storage for local development and deterministic tests.

    Application code supplies generated keys. The adapter still validates every
    key so a future caller cannot turn a storage operation into path traversal.
    Writes use a temporary file and an atomic rename within the configured root.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
        del content_type  # Content type is canonical metadata in PostgreSQL for this adapter.
        await asyncio.to_thread(self._put_sync, object_key, data)

    async def get(self, *, object_key: str) -> bytes:
        path = self._resolve_key(object_key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ObjectStorageError("The object could not be read.") from exc

    async def delete(self, *, object_key: str) -> None:
        path = self._resolve_key(object_key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise ObjectStorageError("The object could not be deleted.") from exc

    def _put_sync(self, object_key: str, data: bytes) -> None:
        path = self._resolve_key(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise ObjectStorageError("The object could not be written.") from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _resolve_key(self, object_key: str) -> Path:
        if not object_key or "\\" in object_key:
            raise ObjectStorageError("The object key is invalid.")
        relative_key = Path(object_key)
        if relative_key.is_absolute() or ".." in relative_key.parts:
            raise ObjectStorageError("The object key is invalid.")
        path = (self._root / relative_key).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ObjectStorageError("The object key is invalid.") from exc
        return path
