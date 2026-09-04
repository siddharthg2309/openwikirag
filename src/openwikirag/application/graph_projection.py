"""Replaceable graph projection and paginated canonical replay."""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.knowledge import KnowledgeArtifact
from openwikirag.infrastructure.models import KnowledgeArtifactRow


class GraphProjectionError(Exception):
    """Projection dependency or immutable identity validation failed."""


class GraphNeighbor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    artifact_id: UUID
    fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class GraphProjection(Protocol):
    async def upsert(self, artifact: KnowledgeArtifact) -> None: ...
    async def neighbors(
        self, *, tenant_id: UUID, entity_id: str, limit: int = 20
    ) -> tuple[GraphNeighbor, ...]: ...


async def rebuild_projection(
    *,
    tenant_id: UUID,
    session: AsyncSession,
    projection: GraphProjection,
) -> int:
    """Replay immutable artifacts in bounded pages; no canonical mutations."""
    cursor: UUID | None = None
    count = 0
    while True:
        query = select(KnowledgeArtifactRow).where(KnowledgeArtifactRow.tenant_id == tenant_id)
        if cursor is not None:
            query = query.where(KnowledgeArtifactRow.id > cursor)
        rows = tuple(await session.scalars(query.order_by(KnowledgeArtifactRow.id).limit(50)))
        if not rows:
            return count
        for row in rows:
            try:
                artifact = KnowledgeArtifact.model_validate(row.payload)
                if (
                    artifact.tenant_id != tenant_id
                    or artifact.id != row.id
                    or artifact.checksum != row.checksum
                    or artifact.manifest_id != row.manifest_id
                    or artifact.document_version_id != row.document_version_id
                ):
                    raise ValueError("Canonical graph row identity mismatch.")
            except ValueError as exc:
                raise GraphProjectionError("Canonical graph artifact is corrupt.") from exc
            await projection.upsert(artifact)
            count += 1
        cursor = rows[-1].id
