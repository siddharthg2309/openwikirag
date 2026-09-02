"""Tenant-scoped persistence for immutable normalized document artifacts."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocumentVersion, NormalizedDocumentArtifact


class NormalizedArtifactRepository:
    """Read and create normalized-artifact metadata in caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_document_version(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
    ) -> DocumentVersion | None:
        version = await self._session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == document_version_id,
                DocumentVersion.tenant_id == tenant_id,
            )
        )
        return version

    async def get_by_parser_identity(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        parser_name: str,
        parser_version: str,
    ) -> NormalizedDocumentArtifact | None:
        artifact = await self._session.scalar(
            select(NormalizedDocumentArtifact).where(
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                NormalizedDocumentArtifact.document_version_id == document_version_id,
                NormalizedDocumentArtifact.parser_name == parser_name,
                NormalizedDocumentArtifact.parser_version == parser_version,
            )
        )
        return artifact

    async def create(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        parser_name: str,
        parser_version: str,
        content_checksum_sha256: str,
        artifact_object_key: str,
        character_count: int,
        span_count: int,
    ) -> NormalizedDocumentArtifact:
        artifact = NormalizedDocumentArtifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            parser_name=parser_name,
            parser_version=parser_version,
            content_checksum_sha256=content_checksum_sha256,
            artifact_object_key=artifact_object_key,
            character_count=character_count,
            span_count=span_count,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact
