"""Authorized WikiRAG regeneration requests and worker validation."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.ingestion import (
    WIKI_REGENERATION_EVENT_TYPE,
    WIKI_REGENERATION_JOB_TYPE,
    IngestionHandler,
    PermanentJobError,
)
from openwikirag.application.wiki_ingestion import WikiIngestionHandler
from openwikirag.infrastructure.models import IngestionJob
from openwikirag.infrastructure.repositories.jobs import JobRepository
from openwikirag.infrastructure.repositories.outbox import OutboxRepository
from openwikirag.infrastructure.repositories.wiki_page_artifacts import (
    WikiPageArtifactRepository,
)
from openwikirag.security.authorization import AuthorizationService, Permission, Principal


class WikiPageRegenerationError(Exception):
    """Base error for regeneration request orchestration."""


class WikiPageRegenerationPersistenceError(WikiPageRegenerationError):
    """Raised when a regeneration job cannot be staged durably."""


@dataclass(frozen=True, slots=True)
class WikiPageRegenerationResult:
    """Durable request identity returned before asynchronous generation runs."""

    job_id: UUID
    source_page_artifact_id: UUID
    tenant_id: UUID
    document_version_id: UUID
    status: str


class WikiPageRegenerationRequestService:
    """Authorize and enqueue one tenant-scoped regeneration request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._authorization = AuthorizationService()
        self._pages = WikiPageArtifactRepository(session)
        self._jobs = JobRepository(session)
        self._outbox = OutboxRepository(session)

    async def request(
        self,
        *,
        principal: Principal,
        artifact_id: UUID,
        request_id: str | None,
    ) -> WikiPageRegenerationResult | None:
        """Create job and outbox rows only after permission and tenant filtering."""

        self._authorization.require(principal, Permission.REVIEW_WIKI_PAGES)
        tenant_id = UUID(principal.tenant_id)
        page = await self._pages.get_by_id(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
        )
        if page is None:
            return None

        try:
            job = await self._jobs.create(
                tenant_id=tenant_id,
                document_version_id=page.document_version_id,
                job_type=WIKI_REGENERATION_JOB_TYPE,
                request_id=request_id,
            )
            await self._outbox.create_event(
                tenant_id=tenant_id,
                aggregate_type="ingestion_job",
                aggregate_id=str(job.id),
                event_type=WIKI_REGENERATION_EVENT_TYPE,
                payload={
                    "ingestion_job_id": str(job.id),
                    "page_artifact_id": str(page.id),
                    "source_page_checksum": page.page_checksum,
                },
            )
        except Exception as exc:
            await self._session.rollback()
            raise WikiPageRegenerationPersistenceError(
                "The WikiRAG regeneration request could not be staged."
            ) from exc

        return WikiPageRegenerationResult(
            job_id=job.id,
            source_page_artifact_id=page.id,
            tenant_id=page.tenant_id,
            document_version_id=page.document_version_id,
            status=job.status,
        )


class WikiRegenerationHandler:
    """Validate the requested source page before running the WikiRAG pipeline."""

    def __init__(
        self,
        session: AsyncSession,
        pipeline: WikiIngestionHandler,
    ) -> None:
        self._session = session
        self._pages = WikiPageArtifactRepository(session)
        self._pipeline = pipeline

    async def handle(self, *, job: IngestionJob, payload: dict[str, object]) -> None:
        """Require a same-tenant, same-version source page before regeneration."""

        page_artifact_id = _payload_uuid(payload, "page_artifact_id")
        source_page_checksum = payload.get("source_page_checksum")
        if not isinstance(source_page_checksum, str) or not source_page_checksum:
            await self._session.rollback()
            raise PermanentJobError("The regeneration source checksum is invalid.")

        page = await self._pages.get_by_id(
            tenant_id=job.tenant_id,
            artifact_id=page_artifact_id,
        )
        if (
            page is None
            or page.document_version_id != job.document_version_id
            or page.page_checksum != source_page_checksum
        ):
            await self._session.rollback()
            raise PermanentJobError("The regeneration source page is outside the job lineage.")

        job.current_step = "validate_source_page"
        await self._session.flush()
        await self._pipeline.handle(job=job, payload=payload)


class WikiJobRouter:
    """Route claimed durable jobs to the matching WikiRAG workflow."""

    def __init__(
        self,
        *,
        ingestion: WikiIngestionHandler,
        regeneration: WikiRegenerationHandler,
    ) -> None:
        self._handlers: dict[str, IngestionHandler] = {
            "ingestion": ingestion,
            WIKI_REGENERATION_JOB_TYPE: regeneration,
        }

    async def handle(self, *, job: IngestionJob, payload: dict[str, object]) -> None:
        handler = self._handlers.get(job.job_type)
        if handler is None:
            raise PermanentJobError("The ingestion job type is not supported.")
        await handler.handle(job=job, payload=payload)


def _payload_uuid(payload: dict[str, object], field_name: str) -> UUID:
    value = payload.get(field_name)
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(
            f"The regeneration payload field '{field_name}' is invalid."
        ) from exc
