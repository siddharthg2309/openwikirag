"""Opt-in real PostgreSQL worker graph and canonical retrieval integration."""

from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_ingestion import (
    FakeTransport,
    create_job_with_source,
    make_message,
    make_service,
)
from apps.api.tests.test_postgres_integration import postgres_session as postgres_session
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from apps.worker.tests.test_knowledge import GRAPH_TEXT
from openwikirag.application.evidence import CanonicalEvidenceResolver
from openwikirag.application.knowledge import KnowledgeArtifact
from openwikirag.application.retrieval import (
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    SearchRequest,
)
from openwikirag.application.search import SearchService
from openwikirag.application.vector_index import InMemoryVectorIndex
from openwikirag.application.wiki_ingestion import WikiIngestionHandler
from openwikirag.infrastructure.models import KnowledgeArtifactRow
from openwikirag.infrastructure.storage import LocalObjectStorage
from openwikirag.security.authorization import Principal, Role


async def test_postgres_worker_graph_commit_and_canonical_search(
    postgres_session: AsyncSession,
    tmp_path: Path,
) -> None:
    session = postgres_session
    storage = LocalObjectStorage(tmp_path / "objects")
    tenant, job = await create_job_with_source(
        session,
        storage,
        source_type="markdown",
        data=GRAPH_TEXT,
    )
    tenant_id, job_id = tenant.id, job.id
    index = InMemoryVectorIndex()
    handler = WikiIngestionHandler(session, storage, index, config_hash="b" * 64)
    transport = FakeTransport()
    transport.new_messages.append(make_message(tenant_id, job_id))
    assert await make_service(session, transport, handler).consume_once() == 1
    await session.refresh(job)
    assert job.status == "succeeded" and transport.acknowledged == ["1-0"]
    row = await session.scalar(
        select(KnowledgeArtifactRow).where(KnowledgeArtifactRow.tenant_id == tenant_id)
    )
    assert row is not None
    artifact = KnowledgeArtifact.model_validate(row.payload)
    assert artifact.checksum == row.checksum and len(artifact.facts) == 3
    result = await SearchService(
        CandidateRetrievalService(InMemoryCandidateIndex(index.points)),
        CanonicalEvidenceResolver(session, storage),
    ).search(
        principal=Principal(str(uuid4()), str(tenant_id), Role.VIEWER),
        request=SearchRequest(tenant_id=tenant_id, query="Aurora", mode="sparse"),
    )
    assert result.hits and all(hit.evidence.payload.tenant_id == tenant_id for hit in result.hits)
    transport.new_messages.append(make_message(tenant_id, job_id))
    assert await make_service(session, transport, handler).consume_once() == 1
    assert len(transport.acknowledged) == 2
