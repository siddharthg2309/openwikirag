"""Resolve projection metadata to tenant-owned immutable source text."""

import hashlib
import json
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.chunk_artifacts import ChunkManifest, ChunkManifestError
from openwikirag.application.chunking import Chunk, ChunkingConfig, ChunkingError
from openwikirag.application.vector_index import VectorPointPayload
from openwikirag.infrastructure.repositories.chunk_artifacts import ChunkManifestArtifactRepository
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError

MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_VARIANTS = 8
MAX_RESOLUTION_BYTES = 64 * 1024 * 1024
_CHUNK_PROVENANCE = (
    "chunk_id",
    "chunk_kind",
    "parent_chunk_id",
    "source_artifact_checksum",
    "content_checksum_sha256",
    "section_path",
    "page_start",
    "page_end",
)


class EvidenceError(Exception):
    """Base class for canonical evidence errors."""


class EvidenceNotFoundError(EvidenceError):
    """The requested evidence is absent, historical, or outside the tenant."""


class EvidenceIntegrityError(EvidenceError):
    """Canonical bytes, metadata, or projection provenance disagree."""


class EvidenceDependencyError(EvidenceError):
    """Canonical evidence storage is temporarily unavailable."""


class ResolvedEvidence(BaseModel):
    """Canonical text with projection explanation and a durable manifest reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest_id: UUID
    payload: VectorPointPayload
    chunk: Chunk

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if any(
            getattr(self.payload, field) != getattr(self.chunk, field)
            for field in _CHUNK_PROVENANCE
        ):
            raise ValueError("Projection provenance does not match canonical chunk.")
        return self


def read_manifest(data: bytes) -> ChunkManifest:
    """Bound parsing and reconstruct the exact versioned canonical contract."""
    if not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES:
        raise EvidenceIntegrityError("The manifest exceeds the parsing limit.")
    try:
        raw = json.loads(data)
        config_data = dict(raw["chunking_config"])
        config_data.pop("schema_version")
        if any(type(value) is not int for value in config_data.values()):
            raise ValueError("Invalid chunk configuration types.")
        if len(raw["chunks"]) > 20_000:
            raise ValueError("Too many manifest chunks.")
        manifest = ChunkManifest(
            source_artifact_checksum=raw["source_artifact_checksum"],
            chunking_config=ChunkingConfig(**config_data),
            chunks=tuple(Chunk.model_validate(item) for item in raw["chunks"]),
        )
        if manifest.canonical_bytes() != data:
            raise ValueError("Manifest canonical bytes or derived metadata differ.")
        return manifest
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ChunkManifestError,
        ChunkingError,
    ) as exc:
        raise EvidenceIntegrityError("The canonical manifest is invalid.") from exc


class CanonicalEvidenceResolver:
    """Request-owned reader; never reuse its verified-object cache across requests."""

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._repository = ChunkManifestArtifactRepository(session)
        self._storage = storage
        self._loaded: dict[UUID, ChunkManifest] = {}
        self._loaded_bytes = 0

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        payload: VectorPointPayload,
        current_only: bool = True,
    ) -> ResolvedEvidence:
        """Recheck ownership/freshness on every lookup, even for cached objects."""
        try:
            payload = VectorPointPayload.model_validate(payload.model_dump(mode="python"))
        except (ValueError, AttributeError) as exc:
            raise EvidenceIntegrityError("Invalid evidence payload.") from exc
        if payload.tenant_id != tenant_id:
            raise EvidenceNotFoundError("Evidence is unavailable.")
        try:
            rows = await self._repository.list_for_evidence(
                tenant_id=tenant_id,
                document_id=payload.document_id,
                document_version_id=payload.document_version_id,
                source_checksum=payload.source_artifact_checksum,
                source_type=payload.source_type,
                pipeline_version=payload.pipeline_version,
                current_only=current_only,
                limit=MAX_MANIFEST_VARIANTS + 1,
            )
        except Exception as exc:
            raise EvidenceDependencyError("Evidence metadata is unavailable.") from exc
        if not rows:
            raise EvidenceNotFoundError("Evidence is unavailable.")
        if len(rows) > MAX_MANIFEST_VARIANTS:
            raise EvidenceIntegrityError("Too many manifest variants for bounded resolution.")
        found: ResolvedEvidence | None = None
        for row in rows:
            manifest = self._loaded.get(row.id)
            if manifest is None:
                try:
                    data = await self._storage.get(object_key=row.artifact_object_key)
                except ObjectStorageError as exc:
                    raise EvidenceDependencyError("The evidence object is unavailable.") from exc
                self._loaded_bytes += len(data)
                if self._loaded_bytes > MAX_RESOLUTION_BYTES:
                    raise EvidenceIntegrityError("Request evidence bytes exceed the limit.")
                if hashlib.sha256(data).hexdigest() != row.manifest_checksum_sha256:
                    raise EvidenceIntegrityError("Manifest checksum does not match metadata.")
                manifest = read_manifest(data)
                if (
                    manifest.source_artifact_checksum != row.source_artifact_checksum
                    or manifest.chunking_config_checksum != row.chunking_config_checksum
                    or manifest.chunk_count != row.chunk_count
                    or manifest.parent_count != row.parent_count
                    or manifest.child_count != row.child_count
                    or row.manifest_schema_version != "chunk-manifest-v1"
                ):
                    raise EvidenceIntegrityError("Manifest lineage/counts do not match metadata.")
                self._loaded[row.id] = manifest
            for chunk in manifest.chunks:
                if chunk.chunk_id != payload.chunk_id:
                    continue
                try:
                    resolved = ResolvedEvidence(manifest_id=row.id, payload=payload, chunk=chunk)
                except ValueError as exc:
                    raise EvidenceIntegrityError("Canonical chunk provenance conflicts.") from exc
                if found is not None and found.chunk != resolved.chunk:
                    raise EvidenceIntegrityError("Chunk identity conflicts across manifests.")
                found = found or resolved
        if found is None:
            raise EvidenceNotFoundError("Evidence is unavailable.")
        return found
