"""Repeatable local proof of the complete non-voice document path."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app.answer_dependencies import get_answer_run_service
from apps.api.app.dependencies import get_object_storage, get_session
from apps.api.app.main import app
from apps.api.app.search_dependencies import get_search_service
from openwikirag.application.answer_runs import AnswerRunService, RunBusyError
from openwikirag.application.answers import (
    AnswerContext,
    DraftAnswer,
    DraftClaim,
    GroundedGenerationService,
)
from openwikirag.application.evidence import CanonicalEvidenceResolver
from openwikirag.application.ingestion import IngestionConsumerService
from openwikirag.application.outbox import OutboxPublisherService
from openwikirag.application.retrieval import CandidateRetrievalService, InMemoryCandidateIndex
from openwikirag.application.search import SearchService
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.vector_index import InMemoryVectorIndex
from openwikirag.application.wiki_ingestion import WikiIngestionHandler
from openwikirag.application.workflow import AnswerWorkflow
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import create_database_engine, create_session_factory
from openwikirag.infrastructure.models import (
    AnswerRun,
    Base,
    ChunkManifestArtifact,
    Document,
    DocumentVersion,
    IngestionJob,
    NormalizedDocumentArtifact,
    OutboxEvent,
    WikiGenerationArtifact,
    WikiPageArtifact,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.storage import LocalObjectStorage
from openwikirag.infrastructure.streams import StreamMessage
from openwikirag.security.authorization import Principal, Role


class InMemoryStream:
    """Small Redis-port adapter that preserves published envelopes for the test."""

    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []
        self.published: list[StreamMessage] = []
        self.acknowledged: list[str] = []
        self.dead_letters: list[tuple[StreamMessage, str]] = []

    async def publish(
        self,
        *,
        stream_name: str,
        event_id: str,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        payload: dict[str, object],
        traceparent: str | None = None,
    ) -> str:
        del stream_name
        message_id = f"{len(self.published) + 1}-0"
        fields = {
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "aggregate_id": aggregate_id,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        }
        if traceparent is not None:
            fields["traceparent"] = traceparent
        message = StreamMessage(message_id=message_id, fields=fields)
        self.messages.append(message)
        self.published.append(message)
        return message_id

    async def ensure_group(self, *, stream_name: str, group_name: str) -> None:
        del stream_name, group_name

    async def read(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        count: int,
        block_ms: int,
    ) -> list[StreamMessage]:
        del stream_name, group_name, consumer_name, block_ms
        messages = self.messages[:count]
        del self.messages[:count]
        return messages

    async def claim_stale(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamMessage]:
        del stream_name, group_name, consumer_name, min_idle_ms, count
        return []

    async def acknowledge(
        self,
        *,
        stream_name: str,
        group_name: str,
        message_id: str,
    ) -> None:
        del stream_name, group_name
        self.acknowledged.append(message_id)

    async def publish_dead_letter(
        self,
        *,
        stream_name: str,
        message: StreamMessage,
        reason: str,
    ) -> str:
        del stream_name
        self.dead_letters.append((message, reason))
        return f"dead-{len(self.dead_letters)}"


class ExactQuoteProvider:
    """Deterministic model-port substitute that exercises server validation."""

    identity = "deterministic-e2e-answer-v1"

    async def generate(self, context: AnswerContext) -> DraftAnswer:
        quote = context.passages[0].excerpt.strip()
        return DraftAnswer(
            status="answered",
            claims=(DraftClaim(evidence_id="E1", quote=quote),),
        )


class InMemoryRunMutex:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        if key in self._keys:
            raise RunBusyError("Answer run is already active.")
        self._keys.add(key)
        try:
            yield
        finally:
            self._keys.remove(key)


@dataclass
class E2EContext:
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalObjectStorage
    tenant_id: UUID
    subject: str
    principal: Principal


@pytest.fixture
async def e2e_context(tmp_path: Path) -> AsyncIterator[E2EContext]:
    engine: AsyncEngine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'non-voice-e2e.db'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    subject = "oidc|non-voice-e2e"
    async with session_factory() as session:
        identity = IdentityRepository(session)
        tenant = await identity.create_tenant("Non-voice E2E Tenant")
        user = await identity.create_user(
            "non-voice-e2e@example.com",
            auth_provider_subject=subject,
        )
        await identity.add_membership(tenant.id, user.id, Role.EDITOR)
        await session.commit()

        storage = LocalObjectStorage(tmp_path / "objects")

        async def session_override() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_object_storage] = lambda: storage
        try:
            yield E2EContext(
                session=session,
                session_factory=session_factory,
                storage=storage,
                tenant_id=tenant.id,
                subject=subject,
                principal=Principal(str(user.id), str(tenant.id), Role.EDITOR),
            )
        finally:
            app.dependency_overrides.pop(get_answer_run_service, None)
            app.dependency_overrides.pop(get_search_service, None)
            app.dependency_overrides.pop(get_object_storage, None)
            app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


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


async def test_non_voice_document_walkthrough(e2e_context: E2EContext) -> None:
    context = e2e_context
    source = (
        b"# Retrieval\nOpenWikiRAG preserves immutable citation provenance.\n"
        b"The answer path reads canonical evidence before release.\n"
    )
    query = "immutable citation provenance"
    headers = {
        "Authorization": f"Bearer {make_token(context.subject, context.tenant_id)}",
        "X-Request-ID": "non-voice-e2e",
    }
    transport = InMemoryStream()
    vector_index = InMemoryVectorIndex()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        upload = await client.post(
            "/api/v1/documents",
            headers=headers,
            files={"upload": ("knowledge.md", source, "text/markdown")},
        )
        assert upload.status_code == 202, upload.text
        uploaded = upload.json()
        document_id = UUID(uploaded["document_id"])
        version_id = UUID(uploaded["document_version_id"])
        job_id = UUID(uploaded["ingestion_job_id"])

        version = await context.session.get(DocumentVersion, version_id)
        assert version is not None
        assert version.document_id == document_id
        assert await context.storage.get(object_key=version.source_object_key) == source
        event = await context.session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(job_id))
        )
        assert event is not None
        assert event.payload_json["document_version_id"] == str(version_id)

        published = await OutboxPublisherService(
            context.session,
            transport,
            stream_name="openwikirag:e2e-ingestion",
        ).publish_pending(limit=10)
        assert [item.event_id for item in published] == [event.id]
        assert event.published_at is not None
        assert len(transport.published) == 1
        assert transport.published[0].fields["event_id"] == str(event.id)

        handler = WikiIngestionHandler(
            context.session,
            context.storage,
            vector_index,
            config_hash="b" * 64,
        )
        consumer = IngestionConsumerService(
            context.session,
            transport,
            handler,
            stream_name="openwikirag:e2e-ingestion",
            group_name="e2e-group",
            consumer_name="e2e-consumer",
            dead_letter_stream_name="openwikirag:e2e-dead-letter",
            lease_seconds=60,
            retry_backoff_base_seconds=5,
            retry_backoff_max_seconds=60,
            block_ms=1,
        )
        await consumer.ensure_group()
        assert await consumer.consume_once() == 1

        job = await context.session.get(IngestionJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.current_step == "complete"
        assert transport.acknowledged == [transport.published[0].message_id]
        assert transport.dead_letters == []

        document = await context.session.get(Document, document_id)
        normalized = await context.session.scalar(
            select(NormalizedDocumentArtifact).where(
                NormalizedDocumentArtifact.document_version_id == version_id
            )
        )
        generation = await context.session.scalar(
            select(WikiGenerationArtifact).where(
                WikiGenerationArtifact.document_version_id == version_id
            )
        )
        page = await context.session.scalar(
            select(WikiPageArtifact).where(WikiPageArtifact.document_version_id == version_id)
        )
        manifest = await context.session.scalar(
            select(ChunkManifestArtifact).where(
                ChunkManifestArtifact.document_version_id == version_id
            )
        )
        assert document is not None and document.current_version_id == version_id
        assert normalized is not None
        assert generation is not None and generation.normalized_artifact_id == normalized.id
        assert page is not None and page.generation_artifact_id == generation.id
        assert manifest is not None and manifest.chunk_count == len(vector_index.points)
        assert manifest.parent_count > 0 and manifest.child_count > 0
        assert all(point.payload.tenant_id == context.tenant_id for point in vector_index.points)

        search = SearchService(
            CandidateRetrievalService(InMemoryCandidateIndex(vector_index.points)),
            CanonicalEvidenceResolver(context.session, context.storage),
        )
        app.dependency_overrides[get_search_service] = lambda: search
        progress = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert progress.status_code == 200
        assert progress.json()["status"] == "succeeded"

        search_response = await client.post(
            "/api/v1/search",
            headers=headers,
            json={"query": query, "mode": "sparse", "limit": 3},
        )
        assert search_response.status_code == 200, search_response.text
        search_payload = search_response.json()
        assert search_payload["hits"]
        search_hit = search_payload["hits"][0]["evidence"]
        assert query in search_hit["chunk"]["text"]
        assert search_hit["payload"]["document_version_id"] == str(version_id)

        workflow = AnswerWorkflow(
            principal=context.principal,
            search=search,
            resolver=lambda: SourceEvidenceResolver(context.session, context.storage),
            generation=GroundedGenerationService(ExactQuoteProvider(), max_attempts=1),
            checkpointer=InMemorySaver(),
        )
        answer_service = AnswerRunService(
            context.session,
            workflow,
            InMemoryRunMutex().hold,
        )
        app.dependency_overrides[get_answer_run_service] = lambda: answer_service

        created = await client.post(
            "/api/v1/answers",
            headers=headers,
            json={"query": query, "mode": "sparse"},
        )
        assert created.status_code == 202, created.text
        run_id = UUID(created.json()["id"])
        completed = await client.post(f"/api/v1/answers/{run_id}/execute", headers=headers)
        assert completed.status_code == 200, completed.text
        answer_payload = completed.json()
        assert answer_payload["status"] == "complete"
        answer = answer_payload["answer"]
        assert answer["status"] == "answered"
        assert answer["citations"]
        citation = answer["citations"][0]
        assert citation["document_version_id"] == str(version_id)
        assert citation["quote"] in source.decode()
        assert f"[{citation['label']}]" in answer["text"]

        stored_run = await context.session.get(AnswerRun, run_id)
        assert stored_run is not None
        assert stored_run.status == "complete"
        assert stored_run.answer_checksum
        stored_manifest = json.loads(
            (
                await context.storage.get(object_key=manifest.artifact_object_key)
            ).decode("utf-8")
        )
        chunk = next(
            item for item in stored_manifest["chunks"] if item["chunk_id"] == citation["chunk_id"]
        )
        offset = citation["start_char"] - chunk["normalized_start_char"]
        assert chunk["text"][offset : offset + len(citation["quote"])] == citation["quote"]
