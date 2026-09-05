"""Private conversation collection and bounded message reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.answers import GroundedAnswer
from openwikirag.application.conversations import (
    ConversationNotFoundError,
    ConversationService,
    ConversationView,
)
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.workflow import validate_current_answer
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import AuthorizationError, Principal

from .dependencies import get_current_principal, get_object_storage, get_session

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=120)


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
