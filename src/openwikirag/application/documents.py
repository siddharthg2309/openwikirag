"""Document upload validation and canonical intake orchestration."""

import hashlib
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.core.tracing import current_traceparent
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.repositories.documents import (
    DocumentIntegrityError,
    DocumentRepository,
)
from openwikirag.infrastructure.repositories.outbox import OutboxRepository
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError
from openwikirag.security.authorization import (
    AuthorizationError,
    AuthorizationService,
    Permission,
    Principal,
)

logger = structlog.get_logger(__name__)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocumentUploadError(Exception):
    """Base error for a rejected or failed document upload."""


class UploadValidationError(DocumentUploadError):
    """Raised when an upload cannot safely enter the document pipeline."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DocumentStorageFailure(DocumentUploadError):
    """Raised when the raw object cannot be persisted."""


class DocumentPersistenceFailure(DocumentUploadError):
    """Raised when canonical metadata cannot be committed."""


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """Validated upload data and canonical metadata derived from it."""

    original_filename: str
    sanitized_filename: str
    source_type: str
    media_type: str
    byte_size: int
    checksum_sha256: str
    data: bytes


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Identifiers returned after canonical intake is committed."""

    document_id: UUID
    document_version_id: UUID
    ingestion_job_id: UUID
    checksum_sha256: str
    byte_size: int
    media_type: str
    sanitized_filename: str


_EXTENSION_DETAILS: dict[str, tuple[str, str, str]] = {
    ".pdf": ("pdf", "application/pdf", "application/pdf"),
    ".docx": ("docx", DOCX_MEDIA_TYPE, DOCX_MEDIA_TYPE),
    ".md": ("markdown", "text/markdown", "text/markdown"),
    ".markdown": ("markdown", "text/markdown", "text/markdown"),
    ".txt": ("text", "text/plain", "text/plain"),
}
_GENERIC_MEDIA_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def sanitize_filename(filename: str) -> str:
    """Return a short display name with no path components or control chars."""

    normalized = unicodedata.normalize("NFKC", filename).replace("\\", "/")
    basename = PurePosixPath(normalized).name
    path = Path(basename)
    suffix = path.suffix.casefold()
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", path.stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._") or "document"
    return f"{stem[:240]}{suffix}"


def validate_upload(
    *,
    filename: str | None,
    declared_media_type: str | None,
    data: bytes,
    max_upload_bytes: int,
) -> ValidatedUpload:
    """Validate content independently from client-controlled MIME metadata."""

    if filename is None or not filename.strip():
        raise UploadValidationError("DOCUMENT_FILENAME_REQUIRED", "A filename is required.")
    if "\x00" in filename:
        raise UploadValidationError("DOCUMENT_FILENAME_INVALID", "The filename is invalid.")
    if len(filename) > 512:
        raise UploadValidationError("DOCUMENT_FILENAME_TOO_LONG", "The filename is too long.")
    if not data:
        raise UploadValidationError("DOCUMENT_EMPTY", "The uploaded file is empty.")
    if len(data) > max_upload_bytes:
        raise UploadValidationError(
            "DOCUMENT_TOO_LARGE",
            f"The uploaded file exceeds the {max_upload_bytes}-byte limit.",
        )

    basename = PurePosixPath(unicodedata.normalize("NFKC", filename).replace("\\", "/")).name
    extension = Path(basename).suffix.casefold()
    details = _EXTENSION_DETAILS.get(extension)
    if details is None:
        raise UploadValidationError(
            "DOCUMENT_UNSUPPORTED_MEDIA_TYPE",
            "The uploaded file type is not supported.",
        )

    source_type, expected_media_type, _ = details
    sniffed_media_type = _sniff_media_type(data, extension)
    if sniffed_media_type != expected_media_type:
        raise UploadValidationError(
            "DOCUMENT_CONTENT_MISMATCH",
            "The file content does not match its filename.",
        )

    declared = (declared_media_type or "").split(";", 1)[0].strip().casefold()
    allowed_declared_types = set(_GENERIC_MEDIA_TYPES)
    allowed_declared_types.add(expected_media_type)
    if source_type in {"markdown", "text"}:
        allowed_declared_types.add("text/plain")
    if source_type == "docx":
        allowed_declared_types.add("application/zip")
    if declared not in allowed_declared_types:
        raise UploadValidationError(
            "DOCUMENT_DECLARED_TYPE_MISMATCH",
            "The declared media type does not match the file content.",
        )

    sanitized = sanitize_filename(basename)
    return ValidatedUpload(
        original_filename=basename,
        sanitized_filename=sanitized,
        source_type=source_type,
        media_type=expected_media_type,
        byte_size=len(data),
        checksum_sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _sniff_media_type(data: bytes, extension: str) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        if extension != ".docx" or not _is_docx_archive(data):
            return "application/zip"
        return DOCX_MEDIA_TYPE
    if b"\x00" in data:
        return "application/octet-stream"
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/markdown" if extension in {".md", ".markdown"} else "text/plain"


def _is_docx_archive(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


class DocumentUploadService:
    """Coordinate policy, validation, object write, and metadata commit."""

    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        max_upload_bytes: int,
    ) -> None:
        self._session = session
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes
        self._documents = DocumentRepository(session)
        self._outbox = OutboxRepository(session)
        self._audit = AuditRepository(session)
        self._authorization = AuthorizationService()

    async def upload(
        self,
        *,
        principal: Principal,
        filename: str | None,
        declared_media_type: str | None,
        data: bytes,
        request_id: str | None,
    ) -> UploadResult:
        tenant_id = UUID(principal.tenant_id)
        try:
            self._authorization.require(
                principal,
                Permission.WRITE_DOCUMENTS,
                resource_tenant_id=principal.tenant_id,
            )
        except AuthorizationError:
            await self._record_failure(
                principal=principal,
                request_id=request_id,
                error_code="DOCUMENT_UPLOAD_FORBIDDEN",
            )
            raise

        try:
            validated = validate_upload(
                filename=filename,
                declared_media_type=declared_media_type,
                data=data,
                max_upload_bytes=self._max_upload_bytes,
            )
        except UploadValidationError as exc:
            await self._record_failure(
                principal=principal,
                request_id=request_id,
                error_code=exc.code,
            )
            raise

        document_id = uuid4()
        version_id = uuid4()
        job_id = uuid4()
        object_key = (
            f"tenants/{tenant_id}/documents/{document_id}/versions/{version_id}/source"
        )

        try:
            await self._storage.put(
                object_key=object_key,
                data=validated.data,
                content_type=validated.media_type,
            )
        except Exception as exc:
            await self._record_failure(
                principal=principal,
                request_id=request_id,
                error_code="DOCUMENT_OBJECT_WRITE_FAILED",
            )
            raise DocumentStorageFailure("The document could not be stored.") from exc

        try:
            created = await self._documents.create_document_version(
                document_id=document_id,
                version_id=version_id,
                job_id=job_id,
                tenant_id=tenant_id,
                title=Path(validated.sanitized_filename).stem,
                original_filename=validated.original_filename,
                sanitized_filename=validated.sanitized_filename,
                source_type=validated.source_type,
                media_type=validated.media_type,
                byte_size=validated.byte_size,
                checksum_sha256=validated.checksum_sha256,
                source_object_key=object_key,
                request_id=request_id,
            )
            await self._outbox.create_event(
                tenant_id=tenant_id,
                aggregate_type="ingestion_job",
                aggregate_id=str(created.job.id),
                event_type="document.ingestion.requested",
                payload={
                    "document_id": str(created.document.id),
                    "document_version_id": str(created.version.id),
                    "ingestion_job_id": str(created.job.id),
                    "source_object_key": object_key,
                    "checksum_sha256": validated.checksum_sha256,
                    "pipeline_version": created.version.pipeline_version,
                },
                traceparent=current_traceparent(),
            )
            await self._audit.record(
                action="document.upload",
                resource_type="document",
                resource_id=str(created.document.id),
                tenant_id=tenant_id,
                actor_user_id=UUID(principal.subject_id),
                request_id=request_id,
                outcome="success",
                metadata={
                    "document_version_id": str(created.version.id),
                    "ingestion_job_id": str(created.job.id),
                    "checksum_sha256": validated.checksum_sha256,
                    "byte_size": validated.byte_size,
                },
            )
            await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            cleanup_failed = False
            try:
                await self._storage.delete(object_key=object_key)
            except ObjectStorageError:
                cleanup_failed = True
                logger.error(
                    "document_upload_cleanup_failed",
                    tenant_id=str(tenant_id),
                    document_id=str(document_id),
                    object_key=object_key,
                )
            await self._record_failure(
                principal=principal,
                request_id=request_id,
                error_code=(
                    "DOCUMENT_METADATA_CONFLICT"
                    if isinstance(exc, DocumentIntegrityError)
                    else "DOCUMENT_METADATA_WRITE_FAILED"
                ),
                metadata={"object_cleanup_failed": cleanup_failed},
            )
            raise DocumentPersistenceFailure("The document could not be registered.") from exc

        return UploadResult(
            document_id=created.document.id,
            document_version_id=created.version.id,
            ingestion_job_id=created.job.id,
            checksum_sha256=validated.checksum_sha256,
            byte_size=validated.byte_size,
            media_type=validated.media_type,
            sanitized_filename=validated.sanitized_filename,
        )

    async def _record_failure(
        self,
        *,
        principal: Principal,
        request_id: str | None,
        error_code: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._audit.record(
                action="document.upload",
                resource_type="document",
                tenant_id=UUID(principal.tenant_id),
                actor_user_id=UUID(principal.subject_id),
                request_id=request_id,
                outcome="failure",
                metadata={"error_code": error_code, **(metadata or {})},
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
