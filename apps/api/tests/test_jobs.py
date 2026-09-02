"""API proof for tenant-scoped ingestion-job progress."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    AuditEvent,
    Base,
    Document,
    DocumentVersion,
    IngestionJob,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authorization import Role


class JobContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        subject: str,
        tenant_id: UUID,
        job_id: UUID,
        foreign_job_id: UUID,
    ) -> None:
        self.session_factory = session_factory
        self.subject = subject
        self.tenant_id = tenant_id
        self.job_id = job_id
        self.foreign_job_id = foreign_job_id


@pytest.fixture
async def job_context(tmp_path: Path) -> AsyncIterator[JobContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    subject = "oidc|job-reader"
    async with session_factory() as session:
        repository = IdentityRepository(session)
        tenant = await repository.create_tenant("Job Reader Tenant")
        foreign_tenant = await repository.create_tenant("Foreign Tenant")
        user = await repository.create_user(
            "job-reader@example.com",
            auth_provider_subject=subject,
        )
        await repository.add_membership(tenant.id, user.id, Role.VIEWER)
        job = await create_job(session, tenant.id, "owned")
        foreign_job = await create_job(session, foreign_tenant.id, "foreign")
        await session.commit()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as test_session:
            yield test_session

    app.dependency_overrides[get_session] = override_get_session
    yield JobContext(session_factory, subject, tenant.id, job.id, foreign_job.id)
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def create_job(session: AsyncSession, tenant_id: UUID, label: str) -> IngestionJob:
    document = Document(
        tenant_id=tenant_id,
        title=f"{label} document",
        source_type="text",
    )
    session.add(document)
    await session.flush()
    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=document.id,
        version_number=1,
        original_filename=f"{label}.txt",
        sanitized_filename=f"{label}.txt",
        source_type="text",
        media_type="text/plain",
        byte_size=5,
        checksum_sha256="a" * 64,
        source_object_key=f"tenants/{tenant_id}/documents/{uuid4()}",
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id
    job = IngestionJob(
        tenant_id=tenant_id,
        document_version_id=version.id,
        available_at=datetime.now(UTC),
    )
    session.add(job)
    await session.flush()
    return job


def make_token(subject: str, tenant_id: UUID) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "tenant_id": str(tenant_id),
        "iss": get_settings().jwt_issuer,
        "aud": get_settings().jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    return jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")


async def get_job(context: JobContext, job_id: UUID, *, token: str | None = "valid") -> Response:
    transport = ASGITransport(app=app)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/v1/jobs/{job_id}", headers=headers)


async def test_viewer_reads_canonical_retryable_state_and_audit(
    job_context: JobContext,
) -> None:
    available_at = datetime.now(UTC) + timedelta(seconds=30)
    async with job_context.session_factory() as session:
        job = await session.get(IngestionJob, job_context.job_id)
        assert job is not None
        job.status = "retryable"
        job.current_step = "retry_wait"
        job.attempts = 2
        job.available_at = available_at
        job.last_error_code = "INGESTION_RETRYABLE_FAILURE"
        job.last_error_message = "The ingestion attempt will be retried."
        await session.commit()

    response = await get_job(
        job_context,
        job_context.job_id,
        token=make_token(job_context.subject, job_context.tenant_id),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == str(job_context.job_id)
    assert payload["status"] == "retryable"
    assert payload["current_step"] == "retry_wait"
    assert payload["progress_percent"] == 0
    assert payload["attempts"] == 2
    assert payload["max_attempts"] == 3
    assert payload["last_error_code"] == "INGESTION_RETRYABLE_FAILURE"
    assert datetime.fromisoformat(payload["available_at"]) == available_at

    async with job_context.session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "job.read",
                AuditEvent.resource_id == str(job_context.job_id),
            )
        )
    assert audit is not None
    assert audit.outcome == "success"


@pytest.mark.parametrize(
    ("job_status", "expected_progress"),
    [
        ("pending", 0),
        ("running", 50),
        ("retryable", 0),
        ("succeeded", 100),
        ("dead_letter", 0),
    ],
)
async def test_progress_percentage_is_a_coarse_lifecycle_mapping(
    job_context: JobContext,
    job_status: str,
    expected_progress: int,
) -> None:
    async with job_context.session_factory() as session:
        job = await session.get(IngestionJob, job_context.job_id)
        assert job is not None
        job.status = job_status
        await session.commit()

    response = await get_job(
        job_context,
        job_context.job_id,
        token=make_token(job_context.subject, job_context.tenant_id),
    )

    assert response.status_code == 200
    assert response.json()["progress_percent"] == expected_progress


async def test_cross_tenant_and_missing_jobs_are_indistinguishable(
    job_context: JobContext,
) -> None:
    token = make_token(job_context.subject, job_context.tenant_id)

    foreign_response = await get_job(job_context, job_context.foreign_job_id, token=token)
    missing_response = await get_job(job_context, uuid4(), token=token)

    assert foreign_response.status_code == 404
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()


async def test_job_progress_requires_bearer_auth(job_context: JobContext) -> None:
    response = await get_job(job_context, job_context.job_id, token=None)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
