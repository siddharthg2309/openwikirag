"""Owner access, persistence, recovery and SSE publish-boundary proof."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from apps.api.app.answer_dependencies import get_answer_run_service
from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from apps.api.tests.test_jobs import make_token
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from apps.worker.tests.test_graph_retrieval import GraphContext
from apps.worker.tests.test_graph_retrieval import graph_context as graph_context
from apps.worker.tests.test_graph_retrieval import session as session
from openwikirag.application.answer_runs import (
    AnswerRunService,
    RunBusyError,
    RunConflictError,
    RunNotFoundError,
)
from openwikirag.application.answers import (
    AnswerContext,
    DraftAnswer,
    DraftClaim,
    GenerationInputError,
    GenerationRetryableError,
    GroundedGenerationService,
)
from openwikirag.application.conversations import (
    ConversationNotFoundError,
    ConversationService,
    UserMemoryService,
)
from openwikirag.application.evidence import EvidenceNotFoundError
from openwikirag.application.retention import purge_due
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.workflow import AnswerRequest, AnswerWorkflow
from openwikirag.infrastructure.models import (
    AnswerRun,
    Conversation,
    Document,
    DocumentVersion,
    User,
    UserMemory,
)
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.infrastructure.run_mutex import PostgresRunMutex
from openwikirag.security.authorization import AuthorizationError, Principal, Role


class Provider:
    identity = "answer-run-provider-v1"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.contexts: list[AnswerContext] = []

    async def generate(self, context: AnswerContext) -> DraftAnswer:
        self.contexts.append(context)
        if self.fail:
            raise GenerationRetryableError("fixture outage")
        return DraftAnswer(
            status="answered",
            claims=(DraftClaim(evidence_id="E1", quote=context.passages[0].excerpt.strip()),),
        )


class MemoryMutex:
    locked: set[str] = set()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        if key in self.locked:
            raise RunBusyError("busy")
        self.locked.add(key)
        try:
            yield
        finally:
            self.locked.remove(key)


@dataclass
class RunContext:
    graph: GraphContext
    mutex: MemoryMutex
    saver: InMemorySaver
    token: str

    def service(
        self, provider: Provider | None = None, principal: Principal | None = None
    ) -> AnswerRunService:
        principal = principal or self.graph.principal
        workflow = AnswerWorkflow(
            principal=principal,
            search=self.graph.service(),
            resolver=lambda: SourceEvidenceResolver(self.graph.session, self.graph.storage),
            generation=GroundedGenerationService(provider or Provider(), max_attempts=1),
            checkpointer=self.saver,
        )
        return AnswerRunService(self.graph.session, workflow, self.mutex.hold)


@pytest.fixture
async def run_context(graph_context: GraphContext) -> RunContext:
    user = await IdentityRepository(graph_context.session).create_user(
        f"answer-{uuid4().hex}@example.com", auth_provider_subject="oidc|answer"
    )
    await IdentityRepository(graph_context.session).add_membership(
        graph_context.tenant_id, user.id, Role.VIEWER
    )
    await graph_context.session.commit()
    graph_context.principal = Principal(str(user.id), str(graph_context.tenant_id), Role.VIEWER)
    return RunContext(
        graph_context,
        MemoryMutex(),
        InMemorySaver(),
        make_token("oidc|answer", graph_context.tenant_id),
    )


async def test_persisted_success_trace_checksum_and_idempotent_execute(
    run_context: RunContext,
) -> None:
    provider = Provider()
    service = run_context.service(provider)
    conversation = await ConversationService(
        run_context.graph.session, run_context.graph.principal
    ).create("Graph questions")
    await ConversationService(run_context.graph.session, run_context.graph.principal).append(
        conversation.id, "user", {"text": "Earlier graph topic"}
    )
    await UserMemoryService(run_context.graph.session, run_context.graph.principal).create(
        "answer style", "concise"
    )
    await run_context.graph.session.commit()
    created = await service.create(
        AnswerRequest(
            query="Aurora",
            mode="sparse",
            graph_hops=1,
            conversation_id=conversation.id,
        )
    )
    assert created.status == "pending" and created.attempts == 0 and created.answer is None
    final = await service.execute(created.id)
    assert final.status == "complete" and final.attempts == 1 and final.answer
    assert (await service.execute(created.id)) == final
    trace = await service.trace(created.id)
    assert trace["completed_stages"] == list(service.workflow.stages)
    row = await run_context.graph.session.get(AnswerRun, created.id)
    assert row is not None and row.answer_checksum and "Aurora" not in str(row.error_code)
    history_service = ConversationService(
        run_context.graph.session, run_context.graph.principal, service.workflow.validate_answer
    )
    history = await history_service.get(conversation.id)
    assert [item.role for item in history.messages] == ["user", "user", "assistant"]
    assert [item.sequence for item in history.messages] == [1, 2, 3]
    assert provider.contexts and "Earlier graph topic" in provider.contexts[0].history
    assert "answer style: concise" in provider.contexts[0].history
    cited = await run_context.graph.session.get(
        DocumentVersion, final.answer.citations[0].document_version_id
    )
    assert cited is not None
    document = await run_context.graph.session.get(Document, cited.document_id)
    assert document is not None
    document.current_version_id = None
    await run_context.graph.session.commit()
    with pytest.raises(EvidenceNotFoundError):
        await history_service.get(conversation.id)


async def test_retryable_run_resumes_and_owner_cannot_be_substituted(
    run_context: RunContext,
) -> None:
    failed = run_context.service(Provider(fail=True))
    created = await failed.create(AnswerRequest(query="Aurora", mode="sparse"))
    with pytest.raises(GenerationRetryableError):
        await failed.execute(created.id)
    assert (await failed.get(created.id)).status == "retryable"
    assert (await run_context.service().execute(created.id)).status == "complete"

    other = await IdentityRepository(run_context.graph.session).create_user(
        f"other-{uuid4().hex}@example.com", auth_provider_subject=f"oidc|{uuid4()}"
    )
    await IdentityRepository(run_context.graph.session).add_membership(
        run_context.graph.tenant_id, other.id, Role.ADMIN
    )
    await run_context.graph.session.commit()
    other_principal = Principal(str(other.id), str(run_context.graph.tenant_id), Role.ADMIN)
    with pytest.raises(RunNotFoundError):
        await run_context.service(principal=other_principal).get(created.id)
    conversation = await ConversationService(
        run_context.graph.session, run_context.graph.principal
    ).create()
    await run_context.graph.session.commit()
    with pytest.raises(RunNotFoundError):
        await run_context.service(principal=other_principal).create(
            AnswerRequest(query="Aurora", conversation_id=conversation.id)
        )
    owned = await run_context.graph.session.get(Conversation, conversation.id)
    assert owned is not None
    owned.next_sequence = 201
    await run_context.graph.session.commit()
    with pytest.raises(RunConflictError):
        await run_context.service().create(
            AnswerRequest(query="Aurora", conversation_id=conversation.id)
        )


async def test_revoked_user_blocks_release_and_no_answer_is_persisted(
    run_context: RunContext,
) -> None:
    ctx = run_context

    class Revoke(Provider):
        async def generate(self, context: AnswerContext) -> DraftAnswer:
            user = await ctx.graph.session.get(User, UUID(ctx.graph.principal.subject_id))
            assert user is not None
            user.is_active = False
            await ctx.graph.session.commit()
            return await super().generate(context)

    service = ctx.service(Revoke())
    created = await service.create(AnswerRequest(query="Aurora", mode="sparse"))
    with pytest.raises(AuthorizationError):
        await service.execute(created.id)
    row = await ctx.graph.session.scalar(select(AnswerRun).where(AnswerRun.id == created.id))
    assert row is not None and row.status == "failed" and row.answer_json is None


async def test_http_json_and_sse_emit_only_progress_then_validated_answer(
    run_context: RunContext,
) -> None:
    ctx = run_context
    service = ctx.service()
    app.dependency_overrides[get_answer_run_service] = lambda: service
    headers = {"Authorization": f"Bearer {ctx.token}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/answers", json={"query": "Aurora", "mode": "sparse"}, headers=headers
            )
            assert created.status_code == 202
            run_id = created.json()["id"]
            streamed = await client.post(f"/api/v1/answers/{run_id}/stream", headers=headers)
            assert streamed.status_code == 200
            text = streamed.text
            assert text.count("event: progress") == 9
            assert text.rfind("event: answer") > text.rfind("event: progress")
            assert "answer-run-provider-v1" in text and "Traceback" not in text
            trace = await client.get(f"/api/v1/answers/{run_id}/trace", headers=headers)
            assert trace.status_code == 200 and "sources" not in trace.text
            busy = await client.post(
                "/api/v1/answers", json={"query": "Aurora", "mode": "sparse"}, headers=headers
            )
            busy_id = busy.json()["id"]
            key = f"answer:{ctx.graph.tenant_id}:{ctx.graph.principal.subject_id}:{busy_id}"
            ctx.mutex.locked.add(key)
            try:
                response = await client.post(f"/api/v1/answers/{busy_id}/stream", headers=headers)
                assert "event: error" in response.text and '"retryable":true' in response.text
            finally:
                ctx.mutex.locked.remove(key)
    finally:
        app.dependency_overrides.pop(get_answer_run_service, None)


async def test_real_postgres_mutex_rejects_concurrent_owner(postgres_url: str) -> None:
    mutex, key = PostgresRunMutex(postgres_url), f"proof-{uuid4()}"
    async with mutex.hold(key):
        with pytest.raises(RunBusyError):
            async with mutex.hold(key):
                pytest.fail("second holder entered")


async def test_conversation_http_owner_boundary(run_context: RunContext) -> None:
    async def session_override() -> AsyncIterator[object]:
        yield run_context.graph.session

    app.dependency_overrides[get_session] = session_override
    headers = {"Authorization": f"Bearer {run_context.token}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/conversations", json={"title": "  My   research "}, headers=headers
            )
            assert created.status_code == 201 and created.json()["title"] == "My research"
            conversation_id = created.json()["id"]
            listed = await client.get("/api/v1/conversations", headers=headers)
            assert listed.status_code == 200 and conversation_id in listed.text
            loaded = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
            assert loaded.status_code == 200 and loaded.json()["messages"] == []
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_trace_rejects_corrupt_checkpoint_values(run_context: RunContext) -> None:
    service = run_context.service(Provider(fail=True))
    created = await service.create(AnswerRequest(query="Aurora", mode="sparse"))
    with pytest.raises(GenerationRetryableError):
        await service.execute(created.id)
    await service.workflow.graph.aupdate_state(
        service.workflow.config(created.id), {"trace": ["private source text"]}
    )
    with pytest.raises(GenerationInputError, match="trace"):
        await service.trace(created.id)


async def test_soft_delete_hides_then_purge_removes_rows_and_checkpoint(
    run_context: RunContext,
) -> None:
    ctx = run_context
    conversation_service = ConversationService(ctx.graph.session, ctx.graph.principal)
    conversation = await conversation_service.create("Temporary")
    memory_service = UserMemoryService(ctx.graph.session, ctx.graph.principal)
    memory = await memory_service.create("tone", "brief")
    await ctx.graph.session.commit()
    run_service = ctx.service()
    run = await run_service.create(
        AnswerRequest(query="Aurora", mode="sparse", conversation_id=conversation.id)
    )
    assert (await run_service.execute(run.id)).status == "complete"
    await conversation_service.delete(conversation.id)
    await memory_service.delete(memory.id)
    await ctx.graph.session.commit()
    with pytest.raises(ConversationNotFoundError):
        await conversation_service.get(conversation.id)
    with pytest.raises(RunNotFoundError):
        await run_service.get(run.id)
    assert await memory_service.list() == ()
    stored_conversation = await ctx.graph.session.get(Conversation, conversation.id)
    stored_memory = await ctx.graph.session.get(UserMemory, memory.id)
    assert stored_conversation is not None and stored_memory is not None
    stored_conversation.purge_after = datetime.now(UTC)
    stored_memory.purge_after = datetime.now(UTC)
    await ctx.graph.session.commit()
    counts = await purge_due(ctx.graph.session, ctx.saver, now=datetime.now(UTC))
    await ctx.graph.session.commit()
    assert counts == (1, 1, 1)
    assert await ctx.graph.session.get(Conversation, conversation.id) is None
    assert await ctx.graph.session.get(UserMemory, memory.id) is None
    assert not (
        await run_service.workflow.graph.aget_state(run_service.workflow.config(run.id))
    ).values


async def test_conversation_deleted_during_inference_stops_publication(
    run_context: RunContext,
) -> None:
    ctx = run_context
    conversations = ConversationService(ctx.graph.session, ctx.graph.principal)
    conversation = await conversations.create("Delete during inference")
    await ctx.graph.session.commit()

    class DeletingProvider(Provider):
        async def generate(self, context: AnswerContext) -> DraftAnswer:
            await conversations.delete(conversation.id)
            await ctx.graph.session.commit()
            return await super().generate(context)

    service = ctx.service(DeletingProvider())
    run = await service.create(
        AnswerRequest(query="Aurora", mode="sparse", conversation_id=conversation.id)
    )
    with pytest.raises(AuthorizationError):
        await service.execute(run.id)
    stored = await ctx.graph.session.get(AnswerRun, run.id)
    assert stored is not None and stored.answer_json is None and stored.status == "failed"


async def test_deleted_memory_cannot_be_used_by_pending_run(run_context: RunContext) -> None:
    ctx = run_context
    conversations = ConversationService(ctx.graph.session, ctx.graph.principal)
    conversation = await conversations.create("Memory withdrawal")
    memories = UserMemoryService(ctx.graph.session, ctx.graph.principal)
    memory = await memories.create("style", "brief")
    await ctx.graph.session.commit()
    provider = Provider()
    service = ctx.service(provider)
    run = await service.create(
        AnswerRequest(query="Aurora", mode="sparse", conversation_id=conversation.id)
    )
    await memories.delete(memory.id)
    await ctx.graph.session.commit()
    with pytest.raises(AuthorizationError, match="Memory consent"):
        await service.execute(run.id)
    assert provider.contexts == []
    stored = await ctx.graph.session.get(AnswerRun, run.id)
    assert stored is not None and stored.context_purge_after is not None
    counts = await purge_due(
        ctx.graph.session, ctx.saver, now=stored.context_purge_after.replace(tzinfo=UTC)
    )
    await ctx.graph.session.commit()
    assert counts[1] == 1
    await ctx.graph.session.refresh(stored)
    assert stored.history_context is None and stored.memory_fingerprint is None
    assert stored.context_purge_after is not None  # remains a permanent invalidation marker
