"""Tenant-scoped immutable graph artifact persistence; caller commits."""

from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ChunkManifestArtifact,
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeArtifactRow,
    NormalizedDocumentArtifact,
)


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def source(
        self,
        *,
        tenant_id: UUID,
        manifest_id: UUID,
        current_ready: bool = False,
    ) -> tuple[ChunkManifestArtifact, DocumentVersion] | None:
        query = (
            select(ChunkManifestArtifact, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == ChunkManifestArtifact.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(
                NormalizedDocumentArtifact,
                NormalizedDocumentArtifact.id == ChunkManifestArtifact.normalized_artifact_id,
            )
            .where(
                ChunkManifestArtifact.id == manifest_id,
                ChunkManifestArtifact.tenant_id == tenant_id,
                DocumentVersion.tenant_id == tenant_id,
                Document.tenant_id == tenant_id,
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                NormalizedDocumentArtifact.document_version_id == DocumentVersion.id,
                NormalizedDocumentArtifact.content_checksum_sha256
                == ChunkManifestArtifact.source_artifact_checksum,
            )
        )
        if current_ready:
            query = query.where(
                Document.current_version_id == DocumentVersion.id,
                exists().where(
                    IngestionJob.tenant_id == tenant_id,
                    IngestionJob.document_version_id == DocumentVersion.id,
                    IngestionJob.job_type == "ingestion",
                    IngestionJob.status == "succeeded",
                ),
            )
        row = (await self.session.execute(query)).first()
        return (row[0], row[1]) if row else None

    async def get(self, *, tenant_id: UUID, artifact_id: UUID) -> KnowledgeArtifactRow | None:
        row: KnowledgeArtifactRow | None = await self.session.scalar(
            select(KnowledgeArtifactRow).where(
                KnowledgeArtifactRow.tenant_id == tenant_id,
                KnowledgeArtifactRow.id == artifact_id,
            )
        )
        return row

    async def put(self, payload: dict[str, Any], checksum: str) -> None:
        tenant_id, artifact_id = UUID(payload["tenant_id"]), UUID(payload["id"])
        existing = await self.get(tenant_id=tenant_id, artifact_id=artifact_id)
        if existing is None:
            try:
                async with self.session.begin_nested():
                    self.session.add(
                        KnowledgeArtifactRow(
                            id=artifact_id,
                            tenant_id=tenant_id,
                            document_version_id=UUID(payload["document_version_id"]),
                            manifest_id=UUID(payload["manifest_id"]),
                            extractor=payload["extractor"],
                            checksum=checksum,
                            payload=payload,
                        )
                    )
                    await self.session.flush()
                return
            except IntegrityError:
                existing = await self.get(tenant_id=tenant_id, artifact_id=artifact_id)
        if existing is None or existing.checksum != checksum or existing.payload != payload:
            raise ValueError("Immutable graph artifact conflict.")
