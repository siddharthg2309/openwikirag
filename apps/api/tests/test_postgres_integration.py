"""Integration proof for the migration and PostgreSQL tenant policies."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.models import (
    AnswerRun,
    AuditEvent,
    ChunkManifestArtifact,
    Conversation,
    ConversationMessage,
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeArtifactRow,
    Membership,
    NormalizedDocumentArtifact,
    UserMemory,
    WikiGenerationArtifact,
    WikiPageArtifact,
)
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.repositories.documents import DocumentRepository
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authentication import JWTAuthenticator
from openwikirag.security.authorization import Role


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = os.environ.get("OPENWIKIRAG_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set OPENWIKIRAG_TEST_POSTGRES_URL to run PostgreSQL integration tests.")

    project_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["OPENWIKIRAG_DATABASE_URL"] = url
    environment["OPENWIKIRAG_MIGRATION_DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=environment,
        check=True,
    )
    return url


@pytest.fixture
async def postgres_session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(postgres_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await session.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'openwikirag_rls_test'
                    ) THEN
                        CREATE ROLE openwikirag_rls_test NOSUPERUSER NOLOGIN;
                    END IF;
                END
                $$
                """
            )
        )
        await session.execute(text("GRANT USAGE ON SCHEMA public TO openwikirag_rls_test"))
        await session.execute(
            text(
                "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                "TO openwikirag_rls_test"
            )
        )
        await session.commit()
        await session.execute(text("SET ROLE openwikirag_rls_test"))
        yield session
    await engine.dispose()


async def test_postgres_rls_filters_memberships_and_audits_by_transaction_tenant(
    postgres_session: AsyncSession,
) -> None:
    repository = IdentityRepository(postgres_session)
    audit = AuditRepository(postgres_session)
    suffix = uuid4().hex
    tenant_a = await repository.create_tenant(f"RLS A {suffix}")
    tenant_b = await repository.create_tenant(f"RLS B {suffix}")
    user = await repository.create_user(
        f"rls-{suffix}@example.com",
        auth_provider_subject=f"oidc|rls-{suffix}",
    )

    await set_tenant_context(postgres_session, tenant_a.id)
    await repository.add_membership(tenant_a.id, user.id, Role.VIEWER)
    await audit.record(
        action="rls.test.a",
        resource_type="tenant",
        tenant_id=tenant_a.id,
        outcome="success",
    )
    await postgres_session.commit()

    await set_tenant_context(postgres_session, tenant_b.id)
    await repository.add_membership(tenant_b.id, user.id, Role.EDITOR)
    await audit.record(
        action="rls.test.b",
        resource_type="tenant",
        tenant_id=tenant_b.id,
        outcome="success",
    )
    await postgres_session.commit()

    await set_tenant_context(postgres_session, tenant_a.id)
    token = JWTAuthenticator(
        secret=get_settings().jwt_secret,
        issuer=get_settings().jwt_issuer,
        audience=get_settings().jwt_audience,
    ).issue_access_token(
        subject=user.auth_provider_subject or "",
        tenant_id=tenant_a.id,
        ttl_seconds=900,
    )

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield postgres_session

    app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"

    await set_tenant_context(postgres_session, tenant_a.id)
    visible_memberships_a = await postgres_session.scalars(
        select(Membership).where(Membership.user_id == user.id)
    )
    visible_audits_a = await postgres_session.scalars(
        select(AuditEvent).where(AuditEvent.action.like("rls.test.%"))
    )
    assert {membership.tenant_id for membership in visible_memberships_a} == {tenant_a.id}
    assert {event.tenant_id for event in visible_audits_a} == {tenant_a.id}
    assert await repository.get_role_for_tenant(user.id, tenant_b.id) is None

    await set_tenant_context(postgres_session, tenant_b.id)
    visible_memberships_b = await postgres_session.scalars(
        select(Membership).where(Membership.user_id == user.id)
    )
    visible_audits_b = await postgres_session.scalars(
        select(AuditEvent).where(AuditEvent.action.like("rls.test.%"))
    )
    assert {membership.tenant_id for membership in visible_memberships_b} == {tenant_b.id}
    assert {event.tenant_id for event in visible_audits_b} == {tenant_b.id}
    assert await repository.get_role_for_tenant(user.id, tenant_b.id) is Role.EDITOR


async def test_postgres_rls_filters_document_intake_rows_and_rejects_foreign_writes(
    postgres_session: AsyncSession,
) -> None:
    repository = IdentityRepository(postgres_session)
    suffix = uuid4().hex
    tenant_a = await repository.create_tenant(f"Document RLS A {suffix}")
    tenant_b = await repository.create_tenant(f"Document RLS B {suffix}")
    document_id = uuid4()
    version_id = uuid4()
    job_id = uuid4()

    await set_tenant_context(postgres_session, tenant_a.id)
    await DocumentRepository(postgres_session).create_document_version(
        document_id=document_id,
        version_id=version_id,
        job_id=job_id,
        tenant_id=tenant_a.id,
        title="Tenant A source",
        original_filename="source.txt",
        sanitized_filename="source.txt",
        source_type="text",
        media_type="text/plain",
        byte_size=4,
        checksum_sha256="a" * 64,
        source_object_key=f"tenants/{tenant_a.id}/source.txt",
        request_id=None,
    )
    await postgres_session.commit()

    await set_tenant_context(postgres_session, tenant_a.id)
    assert (
        await postgres_session.scalar(
            select(Document.id).where(Document.id == document_id)
        )
        is not None
    )
    assert (
        await postgres_session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.id == version_id)
        )
        is not None
    )
    assert (
        await postgres_session.scalar(
            select(IngestionJob.id).where(IngestionJob.id == job_id)
        )
        is not None
    )

    await set_tenant_context(postgres_session, tenant_b.id)
    assert (
        await postgres_session.scalar(
            select(Document.id).where(Document.id == document_id)
        )
        is None
    )

    assert (
        await postgres_session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.id == version_id)
        )
        is None
    )
    assert (
        await postgres_session.scalar(
            select(IngestionJob.id).where(IngestionJob.id == job_id)
        )
        is None
    )

    forged = Document(
        id=uuid4(),
        tenant_id=tenant_a.id,
        title="forged",
        source_type="text",
    )
    postgres_session.add(forged)
    with pytest.raises(DBAPIError):
        await postgres_session.flush()
    await postgres_session.rollback()

    await postgres_session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    assert (
        await postgres_session.scalar(
            select(Document.id).where(Document.id == document_id)
        )
        is None
    )


async def test_postgres_rls_filters_derived_artifacts_and_rejects_foreign_writes(
    postgres_session: AsyncSession,
) -> None:
    repository = IdentityRepository(postgres_session)
    suffix = uuid4().hex
    tenant_a = await repository.create_tenant(f"Derived RLS A {suffix}")
    tenant_b = await repository.create_tenant(f"Derived RLS B {suffix}")
    document_id = uuid4()
    version_id = uuid4()
    job_id = uuid4()
    normalized_id = uuid4()
    manifest_id = uuid4()
    generation_id = uuid4()
    page_id = uuid4()
    knowledge_id = uuid4()

    await set_tenant_context(postgres_session, tenant_a.id)
    await DocumentRepository(postgres_session).create_document_version(
        document_id=document_id,
        version_id=version_id,
        job_id=job_id,
        tenant_id=tenant_a.id,
        title="Derived source",
        original_filename="source.txt",
        sanitized_filename="source.txt",
        source_type="text",
        media_type="text/plain",
        byte_size=4,
        checksum_sha256="a" * 64,
        source_object_key=f"tenants/{tenant_a.id}/derived-source.txt",
        request_id=None,
    )
    normalized = NormalizedDocumentArtifact(
        id=normalized_id,
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        parser_name="text",
        parser_version="v1",
        content_checksum_sha256="b" * 64,
        artifact_object_key=f"tenants/{tenant_a.id}/normalized.json",
        character_count=1,
        span_count=1,
    )
    manifest = ChunkManifestArtifact(
        id=manifest_id,
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        normalized_artifact_id=normalized_id,
        manifest_schema_version="chunk-manifest-v1",
        source_artifact_checksum="b" * 64,
        chunking_config_checksum="c" * 64,
        manifest_checksum_sha256="d" * 64,
        artifact_object_key=f"tenants/{tenant_a.id}/manifest.json",
        chunk_count=1,
        parent_count=1,
        child_count=0,
    )
    generation = WikiGenerationArtifact(
        id=generation_id,
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        normalized_artifact_id=normalized_id,
        base_page_checksum="e" * 64,
        source_artifact_checksum="b" * 64,
        metadata_checksum="f" * 64,
        prompt_checksum="1" * 64,
        config_hash="2" * 64,
        provider_identity="test-provider",
        generation_version="wiki-generation-v1",
        result_checksum_sha256="3" * 64,
        artifact_object_key=f"tenants/{tenant_a.id}/generation.json",
    )
    page = WikiPageArtifact(
        id=page_id,
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        normalized_artifact_id=normalized_id,
        generation_artifact_id=generation_id,
        page_checksum="e" * 64,
        generation_result_checksum_sha256="3" * 64,
        content_checksum_sha256="4" * 64,
        artifact_object_key=f"tenants/{tenant_a.id}/page.json",
    )
    knowledge = KnowledgeArtifactRow(
        id=knowledge_id,
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        manifest_id=manifest_id,
        extractor="relations-v2",
        checksum="5" * 64,
        payload={"schema_version": "knowledge-v1"},
    )
    postgres_session.add(normalized)
    await postgres_session.flush()
    postgres_session.add_all((manifest, generation))
    await postgres_session.flush()
    postgres_session.add_all((page, knowledge))
    await postgres_session.flush()
    await postgres_session.commit()

    derived_rows = (
        (NormalizedDocumentArtifact, normalized_id),
        (ChunkManifestArtifact, manifest_id),
        (WikiGenerationArtifact, generation_id),
        (WikiPageArtifact, page_id),
        (KnowledgeArtifactRow, knowledge_id),
    )
    await set_tenant_context(postgres_session, tenant_a.id)
    for model, artifact_id in derived_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == artifact_id)
        )
        assert visible_id == artifact_id

    await set_tenant_context(postgres_session, tenant_b.id)
    for model, artifact_id in derived_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == artifact_id)
        )
        assert visible_id is None

    forged = NormalizedDocumentArtifact(
        id=uuid4(),
        tenant_id=tenant_a.id,
        document_version_id=version_id,
        parser_name="forged",
        parser_version="v1",
        content_checksum_sha256="6" * 64,
        artifact_object_key=f"tenants/{tenant_a.id}/forged.json",
        character_count=1,
        span_count=1,
    )
    postgres_session.add(forged)
    with pytest.raises(DBAPIError):
        await postgres_session.flush()
    await postgres_session.rollback()

    await postgres_session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    for model, artifact_id in derived_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == artifact_id)
        )
        assert visible_id is None


async def test_postgres_rls_filters_private_rows_and_rejects_foreign_writes(
    postgres_session: AsyncSession,
) -> None:
    repository = IdentityRepository(postgres_session)
    suffix = uuid4().hex
    tenant_a = await repository.create_tenant(f"Private RLS A {suffix}")
    tenant_b = await repository.create_tenant(f"Private RLS B {suffix}")
    owner = await repository.create_user(f"private-rls-{suffix}@example.com")
    conversation_id = uuid4()
    message_id = uuid4()
    answer_run_id = uuid4()
    memory_id = uuid4()

    await set_tenant_context(postgres_session, tenant_a.id)
    conversation = Conversation(
        id=conversation_id,
        tenant_id=tenant_a.id,
        user_id=owner.id,
        title="Private conversation",
    )
    postgres_session.add(conversation)
    await postgres_session.flush()
    postgres_session.add_all(
        (
            ConversationMessage(
                id=message_id,
                conversation_id=conversation_id,
                tenant_id=tenant_a.id,
                user_id=owner.id,
                sequence=1,
                role="user",
                content_json={"text": "private"},
                checksum="a" * 64,
            ),
            AnswerRun(
                id=answer_run_id,
                tenant_id=tenant_a.id,
                user_id=owner.id,
                conversation_id=conversation_id,
                request_json={"query": "private"},
                provider_identity="test-provider",
            ),
            UserMemory(
                id=memory_id,
                tenant_id=tenant_a.id,
                user_id=owner.id,
                memory_key="style",
                memory_value="brief",
            ),
        )
    )
    await postgres_session.flush()
    await postgres_session.commit()

    private_rows = (
        (Conversation, conversation_id),
        (ConversationMessage, message_id),
        (AnswerRun, answer_run_id),
        (UserMemory, memory_id),
    )
    await set_tenant_context(postgres_session, tenant_a.id)
    for model, private_id in private_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == private_id)
        )
        assert visible_id == private_id

    await set_tenant_context(postgres_session, tenant_b.id)
    for model, private_id in private_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == private_id)
        )
        assert visible_id is None

    forged = Conversation(
        id=uuid4(),
        tenant_id=tenant_a.id,
        user_id=owner.id,
        title="forged",
    )
    postgres_session.add(forged)
    with pytest.raises(DBAPIError):
        await postgres_session.flush()
    await postgres_session.rollback()

    await postgres_session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    for model, private_id in private_rows:
        visible_id = await postgres_session.scalar(
            select(model.id).where(model.id == private_id)
        )
        assert visible_id is None
