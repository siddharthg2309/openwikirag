"""Tenant-scoped persistence for immutable chunk-manifest metadata."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChunkManifestArtifact, NormalizedDocumentArtifact


class ChunkManifestArtifactRepository:
    """Read and create chunk-manifest metadata in caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
