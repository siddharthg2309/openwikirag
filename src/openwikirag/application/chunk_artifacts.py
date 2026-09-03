"""Persist deterministic chunk manifests as immutable derived artifacts."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Self
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.chunking import (
    CHUNK_SCHEMA_VERSION,
    Chunk,
    ChunkingConfig,
    ChunkingResult,
)
from openwikirag.infrastructure.models import ChunkManifestArtifact
from openwikirag.infrastructure.repositories.chunk_artifacts import (
    ChunkManifestArtifactRepository,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError

CHUNK_MANIFEST_SCHEMA_VERSION: Literal["chunk-manifest-v1"] = "chunk-manifest-v1"
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ChunkManifestError(Exception):
    """Base error for chunk-manifest construction and persistence failures."""


class ChunkManifestInputError(ChunkManifestError):
    """Raised when a chunk result cannot form a valid manifest."""


class ChunkManifestSourceNotFoundError(ChunkManifestError):
    """Raised when the source artifact is absent from the tenant/version scope."""


class ChunkManifestSourceMismatchError(ChunkManifestError):
    """Raised when chunks do not belong to the selected normalized artifact."""


class ChunkManifestStorageError(ChunkManifestError):
    """Raised when a manifest object cannot be written or verified."""


class ChunkManifestConflictError(ChunkManifestError):
    """Raised when immutable metadata or object bytes disagree with a rerun."""


class ChunkManifestPersistenceError(ChunkManifestError):
    """Raised when metadata commit fails after an object write."""

    def __init__(self, *, cleanup_failed: bool) -> None:
        super().__init__("The chunk manifest metadata could not be recorded.")
        self.cleanup_failed = cleanup_failed


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    """Canonical, self-contained snapshot of one chunking run."""

    source_artifact_checksum: str
    chunking_config: ChunkingConfig
    chunks: tuple[Chunk, ...]

    def __post_init__(self) -> None:
        if not _CHECKSUM_PATTERN.fullmatch(self.source_artifact_checksum):
            raise ChunkManifestInputError("The source artifact checksum is invalid.")
        if not isinstance(self.chunking_config, ChunkingConfig):
            raise ChunkManifestInputError("The chunking configuration is invalid.")
        if not self.chunks:
            raise ChunkManifestInputError("A chunk manifest cannot be empty.")

        parents = {
            chunk.chunk_id: chunk for chunk in self.chunks if chunk.chunk_kind == "parent"
        }
        chunk_ids = tuple(chunk.chunk_id for chunk in self.chunks)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ChunkManifestInputError("Chunk ids must be unique within a manifest.")
        for chunk in self.chunks:
            if chunk.source_artifact_checksum != self.source_artifact_checksum:
                raise ChunkManifestInputError(
                    "Every chunk must reference the manifest source artifact."
                )
            if chunk.chunk_kind == "child":
                if chunk.parent_chunk_id not in parents:
                    raise ChunkManifestInputError("Every child must reference a manifest parent.")
                parent = parents[chunk.parent_chunk_id]
                if not (
                    parent.normalized_start_char <= chunk.normalized_start_char
                    and chunk.normalized_end_char <= parent.normalized_end_char
                ):
                    raise ChunkManifestInputError(
                        "Every child range must be contained by its parent."
                    )

    @classmethod
    def from_result(
        cls,
        *,
        result: ChunkingResult,
        config: ChunkingConfig,
    ) -> Self:
        """Convert a validated chunking result into a canonical manifest."""

        if not isinstance(result, ChunkingResult):
            raise ChunkManifestInputError("Persistence requires a ChunkingResult.")
        if not isinstance(config, ChunkingConfig):
            raise ChunkManifestInputError("Persistence requires a ChunkingConfig.")
        if not result.chunks:
            raise ChunkManifestInputError("Persistence requires at least one chunk.")
        return cls(
            source_artifact_checksum=result.chunks[0].source_artifact_checksum,
            chunking_config=config,
            chunks=result.chunks,
        )

    @property
    def chunking_config_checksum(self) -> str:
        """Return the stable identity of the validated chunking configuration."""

        return self.chunking_config.checksum_sha256

    @property
    def chunk_count(self) -> int:
        """Return the total number of immutable chunks in this manifest."""

        return len(self.chunks)

    @property
    def parent_count(self) -> int:
        """Return the number of structural parent chunks."""

        return sum(chunk.chunk_kind == "parent" for chunk in self.chunks)

    @property
    def child_count(self) -> int:
        """Return the number of bounded child chunks."""

        return sum(chunk.chunk_kind == "child" for chunk in self.chunks)

    def canonical_payload(self) -> dict[str, object]:
        """Return the stable JSON representation stored outside PostgreSQL."""

        return {
            "schema_version": CHUNK_MANIFEST_SCHEMA_VERSION,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "source_artifact_checksum": self.source_artifact_checksum,
            "chunking_config": self.chunking_config.canonical_payload(),
            "chunking_config_checksum": self.chunking_config_checksum,
            "chunk_count": self.chunk_count,
            "parent_count": self.parent_count,
            "child_count": self.child_count,
            "chunks": [chunk.canonical_payload() for chunk in self.chunks],
        }

    def canonical_bytes(self) -> bytes:
        """Serialize the manifest deterministically for immutable persistence."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the checksum of the exact persisted manifest bytes."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PersistedChunkManifest:
    """Canonical manifest identity returned after persistence or safe reuse."""

    artifact_id: UUID
    document_version_id: UUID
    normalized_artifact_id: UUID
    manifest_schema_version: str
    source_artifact_checksum: str
    chunking_config_checksum: str
    manifest_checksum_sha256: str
    artifact_object_key: str
    chunk_count: int
    parent_count: int
    child_count: int
    reused: bool


class ChunkManifestService:
    """Write canonical chunk JSON before committing its metadata index."""

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._storage = storage
        self._artifacts = ChunkManifestArtifactRepository(session)

    async def persist(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        config: ChunkingConfig,
        result: ChunkingResult,
    ) -> PersistedChunkManifest:
        """Persist one manifest without crossing tenant, source, or identity boundaries."""

        try:
            manifest = ChunkManifest.from_result(result=result, config=config)
        except ChunkManifestError:
            await self._session.rollback()
            raise

        source_artifact = await self._artifacts.get_normalized_artifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
        )
        if source_artifact is None:
            await self._session.rollback()
            raise ChunkManifestSourceNotFoundError(
                "The normalized artifact was not found in the tenant/version scope."
            )
        if source_artifact.content_checksum_sha256 != manifest.source_artifact_checksum:
            await self._session.rollback()
            raise ChunkManifestSourceMismatchError(
                "The chunk manifest belongs to a different normalized artifact."
            )

        manifest_bytes = manifest.canonical_bytes()
        manifest_checksum = manifest.checksum_sha256
        object_key = _artifact_object_key(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            manifest=manifest,
        )
        existing = await self._artifacts.get_by_identity(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            manifest_schema_version=CHUNK_MANIFEST_SCHEMA_VERSION,
            source_artifact_checksum=manifest.source_artifact_checksum,
            chunking_config_checksum=manifest.chunking_config_checksum,
        )
        if existing is not None:
            if not _metadata_matches(
                existing,
                manifest_checksum=manifest_checksum,
                object_key=object_key,
                manifest=manifest,
            ):
                await self._session.rollback()
                raise ChunkManifestConflictError(
                    "The chunk manifest identity already has different immutable content."
                )
            existing_result = _result(existing, reused=True)
            try:
                await self._verify_existing_object(existing)
            except ChunkManifestError:
                await self._session.rollback()
                raise
            await self._session.rollback()
            return existing_result

        try:
            await self._storage.put(
                object_key=object_key,
                data=manifest_bytes,
                content_type="application/vnd.openwikirag.chunk-manifest+json",
            )
        except ObjectStorageError as exc:
            await self._session.rollback()
            raise ChunkManifestStorageError(
                "The chunk manifest object could not be stored."
            ) from exc

        try:
            artifact = await self._artifacts.create(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                manifest_schema_version=CHUNK_MANIFEST_SCHEMA_VERSION,
                source_artifact_checksum=manifest.source_artifact_checksum,
                chunking_config_checksum=manifest.chunking_config_checksum,
                manifest_checksum_sha256=manifest_checksum,
                artifact_object_key=object_key,
                chunk_count=manifest.chunk_count,
                parent_count=manifest.parent_count,
                child_count=manifest.child_count,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            winner = await self._artifacts.get_by_identity(
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                normalized_artifact_id=normalized_artifact_id,
                manifest_schema_version=CHUNK_MANIFEST_SCHEMA_VERSION,
                source_artifact_checksum=manifest.source_artifact_checksum,
                chunking_config_checksum=manifest.chunking_config_checksum,
            )
            if winner is not None and _metadata_matches(
                winner,
                manifest_checksum=manifest_checksum,
                object_key=object_key,
                manifest=manifest,
            ):
                try:
                    await self._verify_existing_object(winner)
                except ChunkManifestError:
                    await self._session.rollback()
                    raise
                winner_result = _result(winner, reused=True)
                await self._session.rollback()
                return winner_result
            await self._session.rollback()
            await self._cleanup(object_key)
            raise ChunkManifestConflictError(
                "A concurrent chunk manifest conflicts with this immutable artifact."
            ) from exc
        except Exception as exc:
            await self._session.rollback()
            cleanup_failed = await self._cleanup(object_key)
            raise ChunkManifestPersistenceError(cleanup_failed=cleanup_failed) from exc

        return _result(artifact, reused=False)

    async def _verify_existing_object(self, artifact: ChunkManifestArtifact) -> None:
        try:
            data = await self._storage.get(object_key=artifact.artifact_object_key)
        except ObjectStorageError as exc:
            raise ChunkManifestStorageError(
                "The existing chunk manifest object could not be read."
            ) from exc
        if hashlib.sha256(data).hexdigest() != artifact.manifest_checksum_sha256:
            raise ChunkManifestConflictError(
                "The existing chunk manifest object checksum does not match metadata."
            )

    async def _cleanup(self, object_key: str) -> bool:
        try:
            await self._storage.delete(object_key=object_key)
        except ObjectStorageError:
            return True
        return False


def _artifact_object_key(
    *,
    tenant_id: UUID,
    document_version_id: UUID,
    manifest: ChunkManifest,
) -> str:
    """Return a path containing only application-generated ids and checksums."""

    return (
        f"tenants/{tenant_id}/document-versions/{document_version_id}/chunks/"
        f"{manifest.chunking_config_checksum}/{manifest.checksum_sha256}.json"
    )


def _metadata_matches(
    artifact: ChunkManifestArtifact,
    *,
    manifest_checksum: str,
    object_key: str,
    manifest: ChunkManifest,
) -> bool:
    """Compare all immutable fields needed before reusing a stored manifest."""

    return (
        artifact.manifest_schema_version == CHUNK_MANIFEST_SCHEMA_VERSION
        and artifact.source_artifact_checksum == manifest.source_artifact_checksum
        and artifact.chunking_config_checksum == manifest.chunking_config_checksum
        and artifact.manifest_checksum_sha256 == manifest_checksum
        and artifact.artifact_object_key == object_key
        and artifact.chunk_count == manifest.chunk_count
        and artifact.parent_count == manifest.parent_count
        and artifact.child_count == manifest.child_count
    )


def _result(
    artifact: ChunkManifestArtifact,
    *,
    reused: bool,
) -> PersistedChunkManifest:
    """Map a database row to the application-owned persisted identity."""

    return PersistedChunkManifest(
        artifact_id=artifact.id,
        document_version_id=artifact.document_version_id,
        normalized_artifact_id=artifact.normalized_artifact_id,
        manifest_schema_version=artifact.manifest_schema_version,
        source_artifact_checksum=artifact.source_artifact_checksum,
        chunking_config_checksum=artifact.chunking_config_checksum,
        manifest_checksum_sha256=artifact.manifest_checksum_sha256,
        artifact_object_key=artifact.artifact_object_key,
        chunk_count=artifact.chunk_count,
        parent_count=artifact.parent_count,
        child_count=artifact.child_count,
        reused=reused,
    )
