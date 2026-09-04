"""Canonical evidence read proofs with persisted manifests and tenant negatives."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from apps.worker.tests.test_chunk_artifacts import (
    ChunkArtifactContext,
)
from apps.worker.tests.test_chunk_artifacts import (
    chunk_artifact_context as chunk_artifact_context,
)
from apps.worker.tests.test_vector_index import _request
from openwikirag.application.chunk_artifacts import ChunkManifestService, PersistedChunkManifest
from openwikirag.application.evidence import (
    CanonicalEvidenceResolver,
    EvidenceDependencyError,
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    read_manifest,
)
from openwikirag.application.vector_index import VectorPointPayload
from openwikirag.infrastructure.models import Document, DocumentVersion
from openwikirag.infrastructure.storage import ObjectStorageError


async def _persist(
    context: ChunkArtifactContext,
) -> tuple[PersistedChunkManifest, VectorPointPayload]:
    saved = await ChunkManifestService(context.session, context.storage).persist(
        tenant_id=context.tenant_id,
        document_version_id=context.document_version_id,
        normalized_artifact_id=context.normalized_artifact_id,
        config=context.config,
        result=context.result,
    )
    version = await context.session.get(DocumentVersion, context.document_version_id)
    assert version is not None
    original = (await _request()).build_point().payload.model_dump()
    chunk = context.result.chunks[0]
    for field in (
        "chunk_id",
        "chunk_kind",
        "parent_chunk_id",
        "source_artifact_checksum",
        "content_checksum_sha256",
        "section_path",
        "page_start",
        "page_end",
    ):
        original[field] = getattr(chunk, field)
    original.update(
        tenant_id=context.tenant_id,
        document_id=version.document_id,
        document_version_id=version.id,
        source_type=version.source_type,
        pipeline_version=version.pipeline_version,
    )
    return saved, VectorPointPayload.model_validate(original)


async def test_resolve_reads_exact_canonical_text_once_per_request(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = chunk_artifact_context
    saved, payload = await _persist(ctx)
    getter = AsyncMock(wraps=ctx.storage.get)
    monkeypatch.setattr(ctx.storage, "get", getter)
    resolver = CanonicalEvidenceResolver(ctx.session, ctx.storage)
    first = await resolver.resolve(tenant_id=ctx.tenant_id, payload=payload)
    second = await resolver.resolve(tenant_id=ctx.tenant_id, payload=payload)
    assert first == second
    assert first.chunk == ctx.result.chunks[0]
    assert first.manifest_id == saved.artifact_id
    assert getter.await_count == 1


async def test_foreign_and_missing_lineage_never_read_objects(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = chunk_artifact_context
    _, payload = await _persist(ctx)
    getter = AsyncMock(wraps=ctx.storage.get)
    monkeypatch.setattr(ctx.storage, "get", getter)
    resolver = CanonicalEvidenceResolver(ctx.session, ctx.storage)
    for requested_tenant, candidate in (
        (ctx.foreign_tenant_id, payload),
        (ctx.tenant_id, payload.model_copy(update={"document_id": uuid4()})),
        (ctx.tenant_id, payload.model_copy(update={"document_version_id": uuid4()})),
        (ctx.tenant_id, payload.model_copy(update={"pipeline_version": "different"})),
    ):
        with pytest.raises(EvidenceNotFoundError):
            await resolver.resolve(tenant_id=requested_tenant, payload=candidate)
    getter.assert_not_awaited()


async def test_freshness_rechecked_even_after_object_is_cached(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    ctx = chunk_artifact_context
    _, payload = await _persist(ctx)
    resolver = CanonicalEvidenceResolver(ctx.session, ctx.storage)
    await resolver.resolve(tenant_id=ctx.tenant_id, payload=payload)
    document = await ctx.session.get(Document, payload.document_id)
    assert document is not None
    document.current_version_id = None
    await ctx.session.commit()
    with pytest.raises(EvidenceNotFoundError):
        await resolver.resolve(tenant_id=ctx.tenant_id, payload=payload)
    historical = await resolver.resolve(
        tenant_id=ctx.tenant_id, payload=payload, current_only=False
    )
    assert historical.chunk == ctx.result.chunks[0]


async def test_corrupt_object_and_missing_chunk_are_not_evidence(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    ctx = chunk_artifact_context
    saved, payload = await _persist(ctx)
    with pytest.raises(EvidenceNotFoundError):
        await CanonicalEvidenceResolver(ctx.session, ctx.storage).resolve(
            tenant_id=ctx.tenant_id,
            payload=payload.model_copy(update={"chunk_id": "chunk-" + "f" * 64}),
        )
    await ctx.storage.put(
        object_key=saved.artifact_object_key, data=b"corrupt", content_type="text"
    )
    with pytest.raises(EvidenceIntegrityError, match="checksum"):
        await CanonicalEvidenceResolver(ctx.session, ctx.storage).resolve(
            tenant_id=ctx.tenant_id,
            payload=payload,
        )


async def test_projection_mismatch_fails_before_text_returns(
    chunk_artifact_context: ChunkArtifactContext,
) -> None:
    ctx = chunk_artifact_context
    _, payload = await _persist(ctx)
    with pytest.raises(EvidenceIntegrityError, match="provenance"):
        await CanonicalEvidenceResolver(ctx.session, ctx.storage).resolve(
            tenant_id=ctx.tenant_id,
            payload=payload.model_copy(update={"content_checksum_sha256": "d" * 64}),
        )


async def test_storage_failure_is_retryable_not_missing(
    chunk_artifact_context: ChunkArtifactContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = chunk_artifact_context
    _, payload = await _persist(ctx)
    monkeypatch.setattr(ctx.storage, "get", AsyncMock(side_effect=ObjectStorageError("offline")))
    with pytest.raises(EvidenceDependencyError):
        await CanonicalEvidenceResolver(ctx.session, ctx.storage).resolve(
            tenant_id=ctx.tenant_id,
            payload=payload,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("chunk_count", 1000),
        ("schema_version", "unknown"),
        ("unexpected", True),
        ("source_artifact_checksum", "invalid"),
    ],
)
async def test_manifest_schema_counts_and_extras_fail_closed(
    chunk_artifact_context: ChunkArtifactContext,
    field: str,
    value: object,
) -> None:
    ctx = chunk_artifact_context
    saved, _ = await _persist(ctx)
    data = await ctx.storage.get(object_key=saved.artifact_object_key)
    assert read_manifest(data).chunks == ctx.result.chunks
    raw = json.loads(data)
    raw[field] = value
    with pytest.raises(EvidenceIntegrityError):
        read_manifest(json.dumps(raw, separators=(",", ":"), sort_keys=True).encode())


def test_manifest_invalid_and_oversized_input() -> None:
    for data in (b"invalid", b"[]", b"{}", b"x" * (32 * 1024 * 1024 + 1)):
        with pytest.raises(EvidenceIntegrityError):
            read_manifest(data)
