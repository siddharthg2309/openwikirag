"""Tenant-scoped persistence primitives for immutable WikiRAG generation artifacts."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import NormalizedDocumentArtifact, WikiGenerationArtifact


class WikiGenerationArtifactRepository:
    """Read and create generation metadata in caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_normalized_artifact(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
    ) -> NormalizedDocumentArtifact | None:
        """Load one normalized artifact only inside its tenant/version scope."""

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
        base_page_checksum: str,
        generation_version: str,
        prompt_checksum: str,
        config_hash: str,
        provider_identity: str,
    ) -> WikiGenerationArtifact | None:
        """Find one generation only when all identity and tenant fields match."""

        artifact = await self._session.scalar(
            select(WikiGenerationArtifact).where(
                WikiGenerationArtifact.tenant_id == tenant_id,
                WikiGenerationArtifact.document_version_id == document_version_id,
                WikiGenerationArtifact.normalized_artifact_id == normalized_artifact_id,
                WikiGenerationArtifact.base_page_checksum == base_page_checksum,
                WikiGenerationArtifact.generation_version == generation_version,
                WikiGenerationArtifact.prompt_checksum == prompt_checksum,
                WikiGenerationArtifact.config_hash == config_hash,
                WikiGenerationArtifact.provider_identity == provider_identity,
            )
        )
        return artifact

    async def create(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        base_page_checksum: str,
        source_artifact_checksum: str,
        metadata_checksum: str,
        prompt_checksum: str,
        config_hash: str,
        provider_identity: str,
        generation_version: str,
        result_checksum_sha256: str,
        artifact_object_key: str,
        review_status: str = "draft",
    ) -> WikiGenerationArtifact:
        """Stage one immutable generation metadata row for the outer transaction."""

        artifact = WikiGenerationArtifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            base_page_checksum=base_page_checksum,
            source_artifact_checksum=source_artifact_checksum,
            metadata_checksum=metadata_checksum,
            prompt_checksum=prompt_checksum,
            config_hash=config_hash,
            provider_identity=provider_identity,
            generation_version=generation_version,
            result_checksum_sha256=result_checksum_sha256,
            artifact_object_key=artifact_object_key,
            review_status=review_status,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact
