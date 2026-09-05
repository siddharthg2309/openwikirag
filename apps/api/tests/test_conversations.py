"""Conversation ownership, ordering, checksum and restart persistence."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_postgres_integration import postgres_session as postgres_session
from apps.api.tests.test_postgres_integration import postgres_url as postgres_url
from openwikirag.application.conversations import (
    ConversationNotFoundError,
    ConversationService,
    content_checksum,
)
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.models import ConversationMessage
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authorization import Principal, Role


async def test_real_postgres_history_survives_fresh_session_and_denies_admin_peer(
    postgres_url: str, postgres_session: AsyncSession
) -> None:
    repo = IdentityRepository(postgres_session)
    suffix = uuid4().hex
    tenant = await repo.create_tenant(f"conversation-{suffix}")
    owner = await repo.create_user(f"owner-{suffix}@example.com")
    peer = await repo.create_user(f"peer-{suffix}@example.com")
    await set_tenant_context(postgres_session, tenant.id)
    await repo.add_membership(tenant.id, owner.id, Role.VIEWER)
    await repo.add_membership(tenant.id, peer.id, Role.ADMIN)
    await postgres_session.commit()
    owner_principal = Principal(str(owner.id), str(tenant.id), Role.VIEWER)
    await set_tenant_context(postgres_session, tenant.id)
    service = ConversationService(postgres_session, owner_principal)
    conversation = await service.create("  Durable   history ")
    await service.append(conversation.id, "user", {"text": "first"})
    await service.append(conversation.id, "user", {"text": "second"})
    await postgres_session.commit()

    engine = create_database_engine(postgres_url)
    try:
        async with create_session_factory(engine)() as fresh:
            await fresh.execute(text("SET ROLE openwikirag_rls_test"))
            await set_tenant_context(fresh, tenant.id)
            restored = await ConversationService(fresh, owner_principal).get(conversation.id)
            assert restored.title == "Durable history"
            assert [item.content["text"] for item in restored.messages] == ["first", "second"]
            assert [item.sequence for item in restored.messages] == [1, 2]
            with pytest.raises(ConversationNotFoundError):
                await ConversationService(
                    fresh, Principal(str(peer.id), str(tenant.id), Role.ADMIN)
                ).get(conversation.id)

        async def append_concurrently(value: str) -> None:
            async with create_session_factory(engine)() as concurrent:
                await concurrent.execute(text("SET ROLE openwikirag_rls_test"))
                await set_tenant_context(concurrent, tenant.id)
                await ConversationService(concurrent, owner_principal).append(
                    conversation.id, "user", {"text": value}
                )
                await concurrent.commit()

        await asyncio.gather(append_concurrently("third"), append_concurrently("fourth"))
        async with create_session_factory(engine)() as final_session:
            await final_session.execute(text("SET ROLE openwikirag_rls_test"))
            await set_tenant_context(final_session, tenant.id)
            final = await ConversationService(final_session, owner_principal).get(conversation.id)
            assert [item.sequence for item in final.messages] == [1, 2, 3, 4]
            assert {item.content["text"] for item in final.messages[2:]} == {"third", "fourth"}
            invalid = {"text": "wrong owner"}
            final_session.add(
                ConversationMessage(
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    user_id=peer.id,
                    sequence=5,
                    role="user",
                    content_json=invalid,
                    checksum=content_checksum(invalid),
                )
            )
            with pytest.raises(IntegrityError):
                await final_session.flush()
    finally:
        await engine.dispose()
