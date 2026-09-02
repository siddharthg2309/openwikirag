"""HTTP contract for safe document intake."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.documents import (
    DocumentPersistenceFailure,
    DocumentStorageFailure,
    DocumentUploadService,
    UploadValidationError,
)
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import (
    AuthorizationError,
    PermissionDeniedError,
    Principal,
)

from .dependencies import get_current_principal, get_object_storage, get_session

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
settings = get_settings()


class DocumentUploadResponse(BaseModel):
    """Accepted source metadata; asynchronous delivery is the next slice."""

    document_id: UUID
    document_version_id: UUID
    ingestion_job_id: UUID
    status: str
    checksum_sha256: str
    byte_size: int
    media_type: str
    sanitized_filename: str


def _service(
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> DocumentUploadService:
    return DocumentUploadService(
        session,
        storage,
        max_upload_bytes=settings.max_upload_bytes,
    )


def _validation_status(code: str) -> int:
    if code == "DOCUMENT_TOO_LARGE":
        return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    if code == "DOCUMENT_UNSUPPORTED_MEDIA_TYPE":
        return status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    return status.HTTP_422_UNPROCESSABLE_ENTITY


@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    upload: Annotated[UploadFile, File(description="Supported PDF, DOCX, Markdown, or text file")],
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[DocumentUploadService, Depends(_service)],
) -> DocumentUploadResponse:
    """Validate and register one immutable source version."""

    data = await upload.read(settings.max_upload_bytes + 1)
    try:
        result = await service.upload(
            principal=principal,
            filename=upload.filename,
            declared_media_type=upload.content_type,
            data=data,
            request_id=request.headers.get("X-Request-ID"),
        )
    except UploadValidationError as exc:
        raise HTTPException(
            status_code=_validation_status(exc.code),
            detail=exc.code,
        ) from exc
    except (PermissionDeniedError, AuthorizationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The identity is not authorized to upload documents.",
        ) from exc
    except DocumentStorageFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable.",
        ) from exc
    except DocumentPersistenceFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The document could not be registered.",
        ) from exc

    return DocumentUploadResponse(
        document_id=result.document_id,
        document_version_id=result.document_version_id,
        ingestion_job_id=result.ingestion_job_id,
        status="uploaded",
        checksum_sha256=result.checksum_sha256,
        byte_size=result.byte_size,
        media_type=result.media_type,
        sanitized_filename=result.sanitized_filename,
    )
