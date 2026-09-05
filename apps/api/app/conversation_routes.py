"""Private conversation collection and bounded message reads."""

import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.answers import GroundedAnswer
from openwikirag.application.conversations import (
    ConversationNotFoundError,
    ConversationService,
    ConversationView,
    UserMemoryService,
)
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.workflow import validate_current_answer
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import AuthorizationError, Principal

from .dependencies import get_current_principal, get_object_storage, get_session

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
memory_router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=120)


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=500)


class MemoryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    key: str
    value: str


def failure(exc: Exception) -> HTTPException:
    if isinstance(exc, ConversationNotFoundError):
        return HTTPException(404, "Conversation not found.")
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, "Conversation access is not authorized.")
    if isinstance(exc, ValueError):
        return HTTPException(422, "Conversation data is invalid.")
    return HTTPException(503, "Conversation storage is temporarily unavailable.")


@router.post("", response_model=ConversationView, status_code=201)
async def create_conversation(
    body: CreateConversation,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConversationView:
    try:
        result = await ConversationService(session, principal).create(body.title)
        await AuditRepository(session).record(
            action="conversation.created",
            resource_type="conversation",
            resource_id=str(result.id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            outcome="success",
        )
        await session.commit()
        return result
    except (AuthorizationError, ValueError, SQLAlchemyError) as exc:
        await session.rollback()
        raise failure(exc) from exc


@router.get("", response_model=tuple[ConversationView, ...])
async def list_conversations(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> tuple[ConversationView, ...]:
    try:
        return await ConversationService(session, principal).list()
    except (AuthorizationError, SQLAlchemyError) as exc:
        raise failure(exc) from exc


@router.get("/{conversation_id}", response_model=ConversationView)
async def get_conversation(
    conversation_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> ConversationView:
    try:

        async def validate(answer: GroundedAnswer) -> None:
            await validate_current_answer(
                answer,
                UUID(principal.tenant_id),
                SourceEvidenceResolver(session, storage),
            )

        return await ConversationService(session, principal, validate).get(conversation_id)
    except (ConversationNotFoundError, AuthorizationError, ValueError, SQLAlchemyError) as exc:
        raise failure(exc) from exc


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    try:
        await ConversationService(session, principal).delete(conversation_id)
        await AuditRepository(session).record(
            action="conversation.deleted",
            resource_type="conversation",
            resource_id=str(conversation_id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            outcome="success",
            metadata={"retention_days": 7},
        )
        await session.commit()
        return Response(status_code=204)
    except (ConversationNotFoundError, AuthorizationError, SQLAlchemyError) as exc:
        await session.rollback()
        raise failure(exc) from exc


@memory_router.post("", response_model=MemoryView, status_code=201)
async def create_memory(
    body: MemoryBody,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MemoryView:
    try:
        row = await UserMemoryService(session, principal).create(body.key, body.value)
        await AuditRepository(session).record(
            action="memory.created",
            resource_type="memory",
            resource_id=str(row.id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            outcome="success",
            metadata={"key_checksum": hashlib.sha256(row.memory_key.encode()).hexdigest()},
        )
        await session.commit()
        return MemoryView(id=row.id, key=row.memory_key, value=row.memory_value)
    except (AuthorizationError, ValueError, SQLAlchemyError) as exc:
        await session.rollback()
        raise failure(exc) from exc


@memory_router.get("", response_model=tuple[MemoryView, ...])
async def list_memory(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> tuple[MemoryView, ...]:
    try:
        rows = await UserMemoryService(session, principal).list()
        return tuple(
            MemoryView(id=row.id, key=row.memory_key, value=row.memory_value) for row in rows
        )
    except (AuthorizationError, SQLAlchemyError) as exc:
        raise failure(exc) from exc


@memory_router.delete("/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    try:
        await UserMemoryService(session, principal).delete(memory_id)
        await AuditRepository(session).record(
            action="memory.deleted",
            resource_type="memory",
            resource_id=str(memory_id),
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            outcome="success",
            metadata={"retention_days": 7},
        )
        await session.commit()
        return Response(status_code=204)
    except (ConversationNotFoundError, AuthorizationError, SQLAlchemyError) as exc:
        await session.rollback()
        raise failure(exc) from exc
