"""Private conversations are context records, never canonical enterprise facts."""

import hashlib
import json
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.answer_runs import require_live_identity
from openwikirag.application.answers import GroundedAnswer
from openwikirag.infrastructure.models import Conversation, ConversationMessage
from openwikirag.security.authorization import Principal


class ConversationNotFoundError(Exception):
    pass


class MessageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: dict[str, Any]


class ConversationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    title: str | None
    messages: tuple[MessageView, ...] = Field(max_length=200)


def content_checksum(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        principal: Principal,
        answer_validator: Callable[[GroundedAnswer], Awaitable[None]] | None = None,
    ):
        self.session, self.principal = session, principal
        self.answer_validator = answer_validator
        self.tenant_id, self.user_id = UUID(principal.tenant_id), UUID(principal.subject_id)

    async def create(self, title: str | None = None) -> ConversationView:
        await require_live_identity(self.session, self.principal)
        if title is not None:
            title = " ".join(unicodedata.normalize("NFKC", title).split())
            if not title or len(title) > 120:
                raise ValueError("Conversation title is invalid.")
        row = Conversation(tenant_id=self.tenant_id, user_id=self.user_id, title=title)
        self.session.add(row)
        await self.session.flush()
        return ConversationView(id=row.id, title=row.title, messages=())

    async def _owned(self, conversation_id: UUID, *, lock: bool = False) -> Conversation:
        await require_live_identity(self.session, self.principal)
        query = (
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
                Conversation.user_id == self.user_id,
                Conversation.status == "active",
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        if row is None:
            raise ConversationNotFoundError("Conversation is unavailable.")
        return row

    async def append(
        self, conversation_id: UUID, role: Literal["user", "assistant"], content: dict[str, Any]
    ) -> ConversationMessage:
        row = await self._owned(conversation_id, lock=True)
        if row.next_sequence > 200:
            raise ValueError("Conversation reached its 200-message bound.")
        if role == "user":
            text = content.get("text")
            if (
                not isinstance(text, str)
                or not text
                or len(text) > 4096
                or set(content) != {"text"}
            ):
                raise ValueError("Invalid user message.")
        else:
            if set(content) != {"answer"}:
                raise ValueError("Invalid assistant message.")
            GroundedAnswer.model_validate(content["answer"])
        checksum = content_checksum(content)
        message = ConversationMessage(
            conversation_id=row.id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            sequence=row.next_sequence,
            role=role,
            content_json=content,
            checksum=checksum,
        )
        row.next_sequence += 1
        row.updated_at = datetime.now(UTC)
        self.session.add(message)
        await self.session.flush()
        return message

    async def get(self, conversation_id: UUID) -> ConversationView:
        row = await self._owned(conversation_id)
        messages = (
            await self.session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == row.id,
                    ConversationMessage.tenant_id == self.tenant_id,
                    ConversationMessage.user_id == self.user_id,
                )
                .order_by(ConversationMessage.sequence)
                .limit(201)
            )
        ).all()
        if len(messages) > 200:
            raise ValueError("Conversation history exceeds the read bound.")
        views = []
        for item in messages:
            if content_checksum(item.content_json) != item.checksum:
                raise ValueError("Conversation message checksum mismatch.")
            if item.role == "assistant":
                if self.answer_validator is None:
                    raise ValueError("Assistant messages require current citation validation.")
                answer = GroundedAnswer.model_validate(item.content_json["answer"])
                await self.answer_validator(answer)
            views.append(
                MessageView(
                    id=item.id,
                    sequence=item.sequence,
                    role=cast(Literal["user", "assistant"], item.role),
                    content=item.content_json,
                )
            )
        return ConversationView(id=row.id, title=row.title, messages=tuple(views))

    async def list(self) -> tuple[ConversationView, ...]:
        await require_live_identity(self.session, self.principal)
        rows = (
            await self.session.scalars(
                select(Conversation)
                .where(
                    Conversation.tenant_id == self.tenant_id,
                    Conversation.user_id == self.user_id,
                    Conversation.status == "active",
                )
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(50)
            )
        ).all()
        return tuple(ConversationView(id=row.id, title=row.title, messages=()) for row in rows)
