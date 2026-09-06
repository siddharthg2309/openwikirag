"""Real PostgreSQL tombstone retention and checkpoint purge proof."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_postgres_integration import postgres_session as postgres_session
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from openwikirag.application.conversations import ConversationService, UserMemoryService
from openwikirag.application.retention import purge_due
from openwikirag.infrastructure.checkpoints import checkpoint_store, setup_checkpoints
from openwikirag.infrastructure.database import set_tenant_context
from openwikirag.infrastructure.models import AnswerRun, Conversation, UserMemory
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authorization import Principal, Role


async def test_real_postgres_purge_respects_deadline(
    postgres_url: str, postgres_session: AsyncSession
) -> None:
    session = postgres_session
    repo = IdentityRepository(session)
    tenant = await repo.create_tenant("retention-proof")
    user = await repo.create_user(f"retention-{uuid4()}@example.com")
    await set_tenant_context(session, tenant.id)
    await repo.add_membership(tenant.id, user.id, Role.VIEWER)
    await session.commit()
    principal = Principal(str(user.id), str(tenant.id), Role.VIEWER)
    conversations = ConversationService(session, principal)
    conversation = await conversations.create("purge fixture")
    memory = await UserMemoryService(session, principal).create("style", "brief")
    run_id = uuid4()
    session.add(
        AnswerRun(
            id=run_id,
            tenant_id=tenant.id,
            user_id=user.id,
            conversation_id=conversation.id,
            request_json={"query": "test"},
            provider_identity="fixture",
        )
    )
    await session.commit()
    await conversations.delete(conversation.id)
    await UserMemoryService(session, principal).delete(memory.id)
    await session.commit()
    await setup_checkpoints(postgres_url)
    async with checkpoint_store(postgres_url) as saver:
        # An SDK checkpoint written independently of the canonical transaction.
        from typing import TypedDict

        from langgraph.graph import END, START, StateGraph

        class State(TypedDict):
            value: str

        def identity(state: State) -> State:
            return state

        builder = StateGraph(State)
        builder.add_node("identity", identity)
        builder.add_edge(START, "identity")
        builder.add_edge("identity", END)
        graph = builder.compile(checkpointer=saver)
        config: RunnableConfig = {
            "configurable": {"thread_id": f"ow:{tenant.id}:{user.id}:{run_id}"}
        }
        await graph.ainvoke({"value": "private fixture"}, config)
        now = datetime.now(UTC)
        assert await purge_due(session, saver, now=now) == (0, 0, 0)
        await session.commit()
        assert (await graph.aget_state(config)).values
        purged = await purge_due(session, saver, now=now + timedelta(days=8))
        assert purged[0] >= 1 and purged[1] >= 1 and purged[2] >= 1
        await session.commit()
        assert not (await graph.aget_state(config)).values
        await set_tenant_context(session, tenant.id)
        assert (
            await session.scalar(select(Conversation.id).where(Conversation.id == conversation.id))
            is None
        )
        assert await session.scalar(select(UserMemory.id).where(UserMemory.id == memory.id)) is None
        assert await session.scalar(select(AnswerRun.id).where(AnswerRun.id == run_id)) is None
