"""Transactional persistence for canonical document intake metadata."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Document, DocumentVersion, IngestionJob


class DocumentIntegrityError(Exception):
    """Raised when document metadata violates a relational constraint."""


@dataclass(frozen=True, slots=True)
class CreatedDocument:
    document: Document
    version: DocumentVersion
    job: IngestionJob


class DocumentRepository:
    """Create canonical document state without owning the outer transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_document_version(
        self,
        *,
        document_id: UUID,
        version_id: UUID,
        job_id: UUID,
        tenant_id: UUID,
        title: str,
        original_filename: str,
        sanitized_filename: str,
        source_type: str,
        media_type: str,
        byte_size: int,
        checksum_sha256: str,
        source_object_key: str,
        request_id: str | None,
        pipeline_version: str = "ingestion-v1",
        available_at: datetime | None = None,
    ) -> CreatedDocument:
        """Flush one document, version, and pending job as one unit of work."""

        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            title=title,
            source_type=source_type,
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=1,
            original_filename=original_filename,
            sanitized_filename=sanitized_filename,
            source_type=source_type,
            media_type=media_type,
            byte_size=byte_size,
            checksum_sha256=checksum_sha256,
            source_object_key=source_object_key,
            pipeline_version=pipeline_version,
        )
        job = IngestionJob(
            id=job_id,
            tenant_id=tenant_id,
            document_version_id=version_id,
            request_id=request_id,
        )
        try:
            # The current-version pointer is a nullable FK back to the version
            # table. Flush the rows in dependency order before setting it.
            self._session.add(document)
            await self._session.flush()
            self._session.add_all((version, job))
            await self._session.flush()
            document.current_version_id = version_id
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DocumentIntegrityError("The document metadata violates a constraint.") from exc
        return CreatedDocument(document=document, version=version, job=job)
