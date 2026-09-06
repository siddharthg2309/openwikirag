"""Retry-safe hard purge across canonical conversation rows and checkpoints."""

from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.infrastructure.database import set_tenant_context
from openwikirag.infrastructure.models import AnswerRun, Conversation, Tenant, UserMemory


async def purge_due(
    session: AsyncSession,
    saver: BaseCheckpointSaver[Any],
    *,
    now: datetime,
    batch: int = 100,
) -> tuple[int, int, int]:
    if not 1 <= batch <= 500 or now.tzinfo is None:
        raise ValueError("Invalid purge controls.")

    tenant_ids = tuple(await session.scalars(select(Tenant.id)))
    conversation_count = memory_count = run_count = 0
    for tenant_id in tenant_ids:
        await set_tenant_context(session, tenant_id)
        conversations_purged, memories_purged, runs_purged = await _purge_tenant(
            session, saver, now=now, batch=batch
        )
        conversation_count += conversations_purged
        memory_count += memories_purged
        run_count += runs_purged
    return conversation_count, memory_count, run_count


async def _purge_tenant(
    session: AsyncSession,
    saver: BaseCheckpointSaver[Any],
    *,
    now: datetime,
    batch: int,
) -> tuple[int, int, int]:
    snapshots = (
        await session.scalars(
            select(AnswerRun)
            .where(
                AnswerRun.context_purge_after <= now,
                AnswerRun.memory_fingerprint.is_not(None),
            )
            .order_by(AnswerRun.context_purge_after, AnswerRun.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for run in snapshots:
        await saver.adelete_thread(f"ow:{run.tenant_id}:{run.user_id}:{run.id}")
        run.history_context = None
        run.history_checksum = None
        run.memory_fingerprint = None
        if run.status != "complete":
            run.status = "failed"
            run.error_code = "memory_withdrawn"
    await session.flush()
    conversations = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.status == "deleted", Conversation.purge_after <= now)
            .order_by(Conversation.purge_after, Conversation.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    run_count = 0
    for conversation in conversations:
        run_ids = (
            await session.scalars(
                select(AnswerRun.id).where(AnswerRun.conversation_id == conversation.id)
            )
        ).all()
        for run_id in run_ids:
            await saver.adelete_thread(
                f"ow:{conversation.tenant_id}:{conversation.user_id}:{run_id}"
            )
            run_count += 1
        await session.delete(conversation)
    memory_ids = (
        await session.scalars(
            select(UserMemory.id)
            .where(
                UserMemory.deleted_at.is_not(None),
                UserMemory.purge_after <= now,
                ~exists().where(
                    AnswerRun.tenant_id == UserMemory.tenant_id,
                    AnswerRun.user_id == UserMemory.user_id,
                    AnswerRun.context_purge_after.is_not(None),
                    AnswerRun.memory_fingerprint.is_not(None),
                ),
            )
            .order_by(UserMemory.purge_after, UserMemory.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if memory_ids:
        await session.execute(delete(UserMemory).where(UserMemory.id.in_(memory_ids)))
    await session.flush()
    return len(conversations), len(memory_ids), run_count
