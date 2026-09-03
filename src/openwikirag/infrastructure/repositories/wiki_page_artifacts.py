"""Tenant-scoped persistence primitives for self-contained WikiRAG pages."""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import WikiGenerationArtifact, WikiPageArtifact


class WikiPageArtifactRepository:
    """Read and create page-artifact metadata in caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self,
        *,
        tenant_id: UUID,
        artifact_id: UUID,
    ) -> WikiPageArtifact | None:
        """Load one page artifact only inside the requested tenant scope."""

        return cast(
            WikiPageArtifact | None,
            await self._session.scalar(
                select(WikiPageArtifact).where(
                    WikiPageArtifact.id == artifact_id,
                    WikiPageArtifact.tenant_id == tenant_id,
                )
            ),
        )

    async def get_generation_artifact(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        generation_artifact_id: UUID,
    ) -> WikiGenerationArtifact | None:
        """Load generation metadata only inside its full tenant lineage."""

        return cast(
            WikiGenerationArtifact | None,
            await self._session.scalar(
                select(WikiGenerationArtifact).where(
                    WikiGenerationArtifact.id == generation_artifact_id,
                    WikiGenerationArtifact.tenant_id == tenant_id,
                    WikiGenerationArtifact.document_version_id == document_version_id,
                    WikiGenerationArtifact.normalized_artifact_id == normalized_artifact_id,
                )
            ),
        )

    async def get_by_identity(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        generation_artifact_id: UUID,
        page_checksum: str,
    ) -> WikiPageArtifact | None:
        """Find one page package only when all identity and tenant fields match."""

        return cast(
            WikiPageArtifact | None,
            await self._session.scalar(
                select(WikiPageArtifact).where(
                    WikiPageArtifact.tenant_id == tenant_id,
                    WikiPageArtifact.document_version_id == document_version_id,
                    WikiPageArtifact.normalized_artifact_id == normalized_artifact_id,
                    WikiPageArtifact.generation_artifact_id == generation_artifact_id,
                    WikiPageArtifact.page_checksum == page_checksum,
                )
            ),
        )

    async def create(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        normalized_artifact_id: UUID,
        generation_artifact_id: UUID,
        page_checksum: str,
        generation_result_checksum_sha256: str,
        content_checksum_sha256: str,
        artifact_object_key: str,
        review_status: str = "draft",
    ) -> WikiPageArtifact:
        """Stage one immutable page-artifact metadata row for the outer transaction."""

        artifact = WikiPageArtifact(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            normalized_artifact_id=normalized_artifact_id,
            generation_artifact_id=generation_artifact_id,
            page_checksum=page_checksum,
            generation_result_checksum_sha256=generation_result_checksum_sha256,
            content_checksum_sha256=content_checksum_sha256,
            artifact_object_key=artifact_object_key,
            review_status=review_status,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact
