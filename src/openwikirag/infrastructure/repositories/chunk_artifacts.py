"""Tenant-scoped persistence for immutable chunk-manifest metadata."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChunkManifestArtifact, Document, DocumentVersion, NormalizedDocumentArtifact


class ChunkManifestArtifactRepository:
    """Read and create chunk-manifest metadata in caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_evidence(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        document_version_id: UUID,
        source_checksum: str,
        source_type: str,
        pipeline_version: str,
        current_only: bool = True,
        limit: int = 9,
    ) -> tuple[ChunkManifestArtifact, ...]:
        """Resolve projection lineage through all canonical tenant-owned parents."""
        query = (
            select(ChunkManifestArtifact)
            .join(
                NormalizedDocumentArtifact,
                NormalizedDocumentArtifact.id == ChunkManifestArtifact.normalized_artifact_id,
            )
            .join(DocumentVersion, DocumentVersion.id == ChunkManifestArtifact.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                ChunkManifestArtifact.tenant_id == tenant_id,
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                DocumentVersion.tenant_id == tenant_id,
                Document.tenant_id == tenant_id,
                Document.id == document_id,
                DocumentVersion.id == document_version_id,
                DocumentVersion.source_type == source_type,
                DocumentVersion.pipeline_version == pipeline_version,
                NormalizedDocumentArtifact.document_version_id == document_version_id,
                NormalizedDocumentArtifact.content_checksum_sha256 == source_checksum,
                ChunkManifestArtifact.source_artifact_checksum == source_checksum,
            )
        )
        if current_only:
            query = query.where(Document.current_version_id == DocumentVersion.id)
        rows = await self._session.scalars(query.order_by(ChunkManifestArtifact.id).limit(limit))
        return tuple(rows)

    async def get_normalized_artifact(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
    ) -> NormalizedDocumentArtifact | None:
        """Load the source artifact only inside the requested tenant/version scope."""

        artifact = await self._session.scalar(
            select(NormalizedDocumentArtifact).where(
                NormalizedDocumentArtifact.id == normalized_artifact_id,
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                NormalizedDocumentArtifact.document_version_id == document_version_id,
            )
        )
        return artifact

    async def get_by_identity(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        manifest_schema_version: str,
        source_artifact_checksum: str,
        chunking_config_checksum: str,
    ) -> ChunkManifestArtifact | None:
        """Find one immutable manifest identity without crossing tenant boundaries."""

        artifact = await self._session.scalar(
            select(ChunkManifestArtifact).where(
                ChunkManifestArtifact.tenant_id == tenant_id,
                ChunkManifestArtifact.document_version_id == document_version_id,
                ChunkManifestArtifact.normalized_artifact_id == normalized_artifact_id,
                ChunkManifestArtifact.manifest_schema_version == manifest_schema_version,
                ChunkManifestArtifact.source_artifact_checksum == source_artifact_checksum,
                ChunkManifestArtifact.chunking_config_checksum == chunking_config_checksum,
            )
        )
        return artifact

    async def create(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        manifest_schema_version: str,
        source_artifact_checksum: str,
        chunking_config_checksum: str,
        manifest_checksum_sha256: str,
        artifact_object_key: str,
        chunk_count: int,
        parent_count: int,
        child_count: int,
    ) -> ChunkManifestArtifact:
        """Stage one immutable manifest metadata row for the outer transaction."""

        artifact = ChunkManifestArtifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            manifest_schema_version=manifest_schema_version,
            source_artifact_checksum=source_artifact_checksum,
            chunking_config_checksum=chunking_config_checksum,
            manifest_checksum_sha256=manifest_checksum_sha256,
            artifact_object_key=artifact_object_key,
            chunk_count=chunk_count,
            parent_count=parent_count,
            child_count=child_count,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact
