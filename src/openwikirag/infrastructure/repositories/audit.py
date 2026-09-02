"""Persistence adapter for append-only audit events."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEvent


class AuditRepository:
    """Write audit events inside the caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: str,
        tenant_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            request_id=request_id,
            metadata_json=metadata,
        )
        self._session.add(event)
        await self._session.flush()
        return event
