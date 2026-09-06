"""Run a redacted round-trip smoke against an S3-compatible object store."""

import argparse
import asyncio
import hashlib
import os
import sys
from collections.abc import Sequence
from uuid import uuid4

from openwikirag.infrastructure.storage import ObjectStorageError, S3ObjectStorage

SMOKE_BYTES = b"openwikirag-s3-storage-smoke-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("OPENWIKIRAG_OBJECT_STORE_ENDPOINT_URL", ""),
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("OPENWIKIRAG_OBJECT_STORE_BUCKET", "openwikirag-smoke"),
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("OPENWIKIRAG_OBJECT_STORE_REGION", "us-east-1"),
    )
    parser.add_argument(
        "--access-key-id",
        default=os.environ.get("OPENWIKIRAG_OBJECT_STORE_ACCESS_KEY_ID", ""),
    )
    parser.add_argument(
        "--secret-access-key",
        default=os.environ.get("OPENWIKIRAG_OBJECT_STORE_SECRET_ACCESS_KEY", ""),
    )
    parser.add_argument(
        "--path-style",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    storage = S3ObjectStorage(
        bucket=args.bucket,
        endpoint_url=args.endpoint_url,
        region=args.region,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        path_style=args.path_style,
    )
    object_key = f"_openwikirag_smoke/{uuid4().hex}.bin"
    await storage.put(
        object_key=object_key,
        data=SMOKE_BYTES,
        content_type="application/octet-stream",
    )
    try:
        stored = await storage.get(object_key=object_key)
        if stored != SMOKE_BYTES:
            raise ObjectStorageError("The S3 smoke returned different bytes.")
    finally:
        await storage.delete(object_key=object_key)
    try:
        await storage.get(object_key=object_key)
    except ObjectStorageError:
        pass
    else:
        raise ObjectStorageError("The deleted S3 smoke object was still readable.")
    print(
        "s3_storage_smoke_passed "
        f"bytes={len(SMOKE_BYTES)} "
        f"sha256={hashlib.sha256(SMOKE_BYTES).hexdigest()} deleted=true"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (ObjectStorageError, ValueError) as exc:
        print(f"s3_storage_smoke_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
