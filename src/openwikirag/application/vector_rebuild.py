"""Replay canonical normalized artifacts into the derived vector projection."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.embeddings import DenseEmbeddingProvider
from openwikirag.application.extraction import (
    InvalidProvenanceError,
    read_normalized_document,
)
from openwikirag.application.metadata import (
    DeterministicMetadataExtractor,
    MetadataExtractionError,
)
from openwikirag.application.vector_index import VectorIndex
from openwikirag.application.vector_ingestion import (
    VectorIngestionConfig,
    VectorIngestionError,
    VectorIngestionService,
)
from openwikirag.infrastructure.models import (
    Document,
    DocumentVersion,
    NormalizedDocumentArtifact,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError


class VectorProjectionRebuildError(Exception):
    """Canonical vector replay could not safely complete."""


@dataclass(frozen=True, slots=True)
class VectorProjectionRebuildResult:
    """Counts from one tenant-scoped replay run."""

    normalized_artifacts: int
    manifests_reused: int
    points_created: int
    points_reused: int


async def rebuild_vector_projection(
    *,
    tenant_id: UUID,
    session: AsyncSession,
    storage: ObjectStorage,
    vector_index: VectorIndex,
    config: VectorIngestionConfig | None = None,
    dense_provider: DenseEmbeddingProvider | None = None,
) -> VectorProjectionRebuildResult:
    """Replay tenant-owned normalized artifacts in bounded UUID pages."""

    if not isinstance(tenant_id, UUID):
        raise VectorProjectionRebuildError("Vector replay requires a UUID tenant id.")

    vector_ingestion = VectorIngestionService(
        session,
        storage,
        vector_index,
        config=config,
        dense_provider=dense_provider,
    )
    metadata_extractor = DeterministicMetadataExtractor()
    cursor: UUID | None = None
    artifact_count = 0
    manifests_reused = 0
    points_created = 0
    points_reused = 0

    while True:
        query = (
            select(NormalizedDocumentArtifact, DocumentVersion, Document)
            .join(
                DocumentVersion,
                DocumentVersion.id == NormalizedDocumentArtifact.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                DocumentVersion.tenant_id == tenant_id,
                Document.tenant_id == tenant_id,
            )
        )
        if cursor is not None:
            query = query.where(NormalizedDocumentArtifact.id > cursor)
        rows = tuple(
            (await session.execute(
                query.order_by(NormalizedDocumentArtifact.id).limit(50)
            )).all()
        )
        if not rows:
            return VectorProjectionRebuildResult(
                normalized_artifacts=artifact_count,
                manifests_reused=manifests_reused,
                points_created=points_created,
                points_reused=points_reused,
            )

        for normalized_row, version, document_row in rows:
            normalized_artifact_id = normalized_row.id
            if (
                normalized_row.tenant_id != tenant_id
                or version.tenant_id != tenant_id
                or document_row.tenant_id != tenant_id
                or version.document_id != document_row.id
            ):
                raise VectorProjectionRebuildError("Canonical vector lineage crosses tenant scope.")
            try:
                data = await storage.get(object_key=normalized_row.artifact_object_key)
                if hashlib.sha256(data).hexdigest() != normalized_row.content_checksum_sha256:
                    raise VectorProjectionRebuildError(
                        "The normalized artifact checksum does not match metadata."
                    )
                normalized = read_normalized_document(data)
                if (
                    normalized.source_type != version.source_type
                    or normalized.parser_name != normalized_row.parser_name
                    or normalized.parser_version != normalized_row.parser_version
                    or normalized.checksum_sha256 != normalized_row.content_checksum_sha256
                    or len(normalized.text) != normalized_row.character_count
                    or len(normalized.spans) != normalized_row.span_count
                ):
                    raise VectorProjectionRebuildError(
                        "The normalized artifact lineage or counts do not match metadata."
                    )
                metadata = metadata_extractor.extract(document=normalized)
                projection = await vector_ingestion.project(
                    tenant_id=tenant_id,
                    document_id=document_row.id,
                    document_version_id=version.id,
                    normalized_artifact_id=normalized_artifact_id,
                    document=normalized,
                    metadata=metadata,
                    pipeline_version=version.pipeline_version,
                )
            except VectorProjectionRebuildError:
                raise
            except (
                ObjectStorageError,
                InvalidProvenanceError,
                MetadataExtractionError,
                VectorIngestionError,
            ) as exc:
                raise VectorProjectionRebuildError(
                    "The canonical normalized artifact could not be replayed."
                ) from exc
            artifact_count += 1
            manifests_reused += int(projection.manifest_reused)
            points_created += projection.points_created
            points_reused += projection.points_reused
            cursor = normalized_artifact_id
