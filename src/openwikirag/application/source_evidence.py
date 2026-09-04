"""Canonical evidence independent of vector or graph representation geometry."""

import hashlib
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.chunking import Chunk
from openwikirag.application.evidence import (
    EvidenceDependencyError,
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    ResolvedEvidence,
    read_manifest,
)
from openwikirag.infrastructure.repositories.knowledge import KnowledgeRepository
from openwikirag.infrastructure.storage import ObjectStorage


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    manifest_id: UUID
    chunk: Chunk

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            f"{self.tenant_id}:{self.document_version_id}:{self.chunk.chunk_id}".encode()
        ).hexdigest()

    @classmethod
    def from_retrieval(cls, evidence: ResolvedEvidence) -> "SourceEvidence":
        return cls(
            tenant_id=evidence.payload.tenant_id,
            document_id=evidence.payload.document_id,
            document_version_id=evidence.payload.document_version_id,
            manifest_id=evidence.manifest_id,
            chunk=evidence.chunk,
        )


class SourceEvidenceResolver:
    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self.repository, self.storage = KnowledgeRepository(session), storage
        self.bytes_read = 0

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        manifest_id: UUID,
        chunk_id: str,
    ) -> SourceEvidence:
        try:
            source = await self.repository.source(
                tenant_id=tenant_id, manifest_id=manifest_id, current_ready=True
            )
            if source is None:
                raise EvidenceNotFoundError("Source evidence is unavailable.")
            row, version = source
            data = await self.storage.get(object_key=row.artifact_object_key)
        except EvidenceNotFoundError:
            raise
        except Exception as exc:
            raise EvidenceDependencyError("Source evidence dependency failed.") from exc
        self.bytes_read += len(data)
        if self.bytes_read > 64 * 1024 * 1024:
            raise EvidenceIntegrityError("Source evidence byte budget exceeded.")
        if hashlib.sha256(data).hexdigest() != row.manifest_checksum_sha256:
            raise EvidenceIntegrityError("Source manifest checksum mismatch.")
        manifest = read_manifest(data)
        if (
            manifest.source_artifact_checksum != row.source_artifact_checksum
            or manifest.chunking_config_checksum != row.chunking_config_checksum
            or manifest.chunk_count != row.chunk_count
            or manifest.parent_count != row.parent_count
            or manifest.child_count != row.child_count
            or row.manifest_schema_version != "chunk-manifest-v1"
        ):
            raise EvidenceIntegrityError("Source manifest lineage mismatch.")
        for chunk in manifest.chunks:
            if chunk.chunk_id == chunk_id:
                return SourceEvidence(
                    tenant_id=tenant_id,
                    document_id=version.document_id,
                    document_version_id=version.id,
                    manifest_id=manifest_id,
                    chunk=chunk,
                )
        raise EvidenceNotFoundError("Source chunk is unavailable.")
