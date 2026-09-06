"""Replaceable object-storage ports and filesystem/S3-compatible adapters."""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from openwikirag.core.config import Settings


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


def _validate_object_key(object_key: str) -> str:
    """Reject keys that could escape the application-owned object namespace."""

    if not object_key or "\\" in object_key:
        raise ObjectStorageError("The object key is invalid.")
    relative_key = Path(object_key)
    if relative_key.is_absolute() or ".." in relative_key.parts:
        raise ObjectStorageError("The object key is invalid.")
    return object_key


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
        _validate_object_key(object_key)
        relative_key = Path(object_key)
        path = (self._root / relative_key).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ObjectStorageError("The object key is invalid.") from exc
        return path


class S3ObjectStorage:
    """S3-compatible storage with bounded blocking SDK isolation.

    boto3 is synchronous, so all provider calls run in worker threads. The
    adapter accepts an injected client for deterministic tests and supports
    MinIO through an explicit endpoint and path-style addressing.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        path_style: bool = True,
        client: Any | None = None,
    ) -> None:
        if not bucket.strip():
            raise ObjectStorageError("The object-storage bucket is invalid.")
        if bool(access_key_id.strip()) != bool(secret_access_key.strip()):
            raise ObjectStorageError(
                "Object-storage access key and secret must be configured together."
            )
        self._bucket = bucket
        self._client = client or self._create_client(
            endpoint_url=endpoint_url,
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            path_style=path_style,
        )
        self._bucket_ready = False
        self._bucket_lock = asyncio.Lock()

    @staticmethod
    def _create_client(
        *,
        endpoint_url: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        path_style: bool,
    ) -> Any:
        client_kwargs: dict[str, Any] = {
            "region_name": region,
            "config": Config(
                s3={"addressing_style": "path" if path_style else "auto"},
            ),
        }
        if endpoint_url.strip():
            client_kwargs["endpoint_url"] = endpoint_url.strip()
        if access_key_id.strip():
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key
        return boto3.client("s3", **client_kwargs)

    async def put(self, *, object_key: str, data: bytes, content_type: str) -> None:
        key = _validate_object_key(object_key)
        await self._ensure_bucket()
        await asyncio.to_thread(self._put_sync, key, data, content_type)

    async def get(self, *, object_key: str) -> bytes:
        key = _validate_object_key(object_key)
        return await asyncio.to_thread(self._get_sync, key)

    async def delete(self, *, object_key: str) -> None:
        key = _validate_object_key(object_key)
        await asyncio.to_thread(self._delete_sync, key)

    async def ensure_ready(self) -> None:
        """Ensure the configured bucket exists before an operator smoke."""

        await self._ensure_bucket()

    async def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        async with self._bucket_lock:
            if self._bucket_ready:
                return
            await asyncio.to_thread(self._ensure_bucket_sync)
            self._bucket_ready = True

    def _ensure_bucket_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code not in {"404", "NoSuchBucket", "NotFound"}:
                raise ObjectStorageError("The object-storage bucket could not be checked.") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError("The object-storage bucket could not be checked.") from exc

        create_kwargs: dict[str, Any] = {"Bucket": self._bucket}
        region = str(self._client.meta.region_name or "us-east-1")
        if region != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            self._client.create_bucket(**create_kwargs)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                raise ObjectStorageError("The object-storage bucket could not be created.") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError("The object-storage bucket could not be created.") from exc

    def _put_sync(self, object_key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
                ContentLength=len(data),
            )
            head = self._client.head_object(Bucket=self._bucket, Key=object_key)
            stored_length = int(head["ContentLength"])
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as exc:
            raise ObjectStorageError("The object could not be written or verified.") from exc
        if stored_length != len(data):
            raise ObjectStorageError("The stored object length could not be verified.")

    def _get_sync(self, object_key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            body = response["Body"]
            try:
                return cast(bytes, body.read())
            finally:
                body.close()
        except (BotoCoreError, ClientError, KeyError, TypeError, OSError) as exc:
            raise ObjectStorageError("The object could not be read.") from exc

    def _delete_sync(self, object_key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("The object could not be deleted.") from exc


def build_object_storage(settings: Settings) -> ObjectStorage:
    """Build the configured adapter for an API, worker, or replay process."""

    if settings.object_storage_backend == "filesystem":
        return LocalObjectStorage(Path(settings.object_store_root))
    return S3ObjectStorage(
        bucket=settings.object_store_bucket,
        endpoint_url=settings.object_store_endpoint_url,
        region=settings.object_store_region,
        access_key_id=settings.object_store_access_key_id,
        secret_access_key=settings.object_store_secret_access_key,
        path_style=settings.object_store_path_style,
    )
