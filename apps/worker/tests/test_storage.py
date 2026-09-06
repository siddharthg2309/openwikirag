"""Object-storage adapter and composition contracts."""

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from openwikirag.core.config import Settings
from openwikirag.infrastructure.storage import (
    LocalObjectStorage,
    ObjectStorageError,
    S3ObjectStorage,
    build_object_storage,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "ObjectStorageOperation")


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._body = BytesIO(data)
        self.closed = False

    def read(self) -> bytes:
        return self._body.read()

    def close(self) -> None:
        self.closed = True
        self._body.close()


class FakeS3Client:
    def __init__(self) -> None:
        self.meta = SimpleNamespace(region_name="us-east-1")
        self.buckets: set[str] = set()
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.create_calls = 0
        self.head_length_override: int | None = None

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise _client_error("404")

    def create_bucket(self, *, Bucket: str, **kwargs: Any) -> None:
        del kwargs
        self.buckets.add(Bucket)
        self.create_calls += 1

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[kwargs["Key"]] = (body, kwargs["ContentType"])

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        data, content_type = self.objects[Key]
        return {
            "ContentLength": self.head_length_override
            if self.head_length_override is not None
            else len(data),
            "ContentType": content_type,
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, FakeBody]:
        del Bucket
        try:
            data, _ = self.objects[Key]
        except KeyError as exc:
            raise _client_error("NoSuchKey") from exc
        return {"Body": FakeBody(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.objects.pop(Key, None)


@pytest.mark.asyncio
async def test_s3_round_trip_ensures_bucket_and_preserves_content_type() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(bucket="openwikirag", client=client)

    await storage.put(
        object_key="tenants/a/source.bin",
        data=b"source bytes",
        content_type="application/octet-stream",
    )
    await storage.put(
        object_key="tenants/a/second.bin",
        data=b"second",
        content_type="text/plain",
    )

    assert client.create_calls == 1
    assert client.put_calls[0]["ContentType"] == "application/octet-stream"
    assert await storage.get(object_key="tenants/a/source.bin") == b"source bytes"

    await storage.delete(object_key="tenants/a/source.bin")
    with pytest.raises(ObjectStorageError, match="could not be read"):
        await storage.get(object_key="tenants/a/source.bin")


@pytest.mark.asyncio
async def test_s3_length_mismatch_fails_closed() -> None:
    client = FakeS3Client()
    client.head_length_override = 999
    storage = S3ObjectStorage(bucket="openwikirag", client=client)

    with pytest.raises(ObjectStorageError, match="length"):
        await storage.put(object_key="tenant/object", data=b"bytes", content_type="text/plain")


@pytest.mark.asyncio
async def test_s3_rejects_unsafe_keys_without_provider_calls() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(bucket="openwikirag", client=client)

    for object_key in ("", "../escape", "nested/../../escape", r"nested\\escape"):
        with pytest.raises(ObjectStorageError, match="invalid"):
            await storage.put(object_key=object_key, data=b"bytes", content_type="text/plain")

    assert client.create_calls == 0
    assert client.put_calls == []


def test_storage_factory_preserves_filesystem_default_and_builds_s3() -> None:
    filesystem = build_object_storage(Settings())
    assert isinstance(filesystem, LocalObjectStorage)

    s3 = build_object_storage(
        Settings(
            object_storage_backend="s3",
            object_store_endpoint_url="http://127.0.0.1:9000",
            object_store_access_key_id="minio",
            object_store_secret_access_key="minio-secret",
        )
    )
    assert isinstance(s3, S3ObjectStorage)


def test_s3_credentials_must_be_configured_as_a_pair() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        Settings(object_store_access_key_id="only-access-key")


def test_production_s3_endpoint_must_use_https() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            environment="production",
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="openwikirag",
            oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
            object_storage_backend="s3",
            object_store_endpoint_url="http://minio.internal:9000",
        )
