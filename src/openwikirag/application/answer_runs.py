"""Durable owner-private answer execution; client disconnects do not erase runs."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.answers import (
    GenerationInputError,
    GenerationOutputError,
    GroundedAnswer,
    pack_context,
)
from openwikirag.application.evidence import EvidenceIntegrityError, EvidenceNotFoundError
from openwikirag.application.workflow import AnswerRequest, AnswerWorkflow
from openwikirag.infrastructure.database import set_tenant_context
from openwikirag.infrastructure.models import AnswerRun, Membership, Tenant, User
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.security.authorization import (
    AuthorizationError,
    AuthorizationService,
    Permission,
    Principal,
    Role,
)


class RunNotFoundError(Exception):
    """Run is absent or belongs to another owner."""


class RunBusyError(Exception):
    """Another process holds the run's execution lock."""


class RunConflictError(Exception):
    """Run cannot be executed with these settings or status."""


RunStatus = Literal["pending", "running", "retryable", "complete", "failed"]


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: RunStatus
    attempts: int
    error_code: str | None
    answer: GroundedAnswer | None = None


def answer_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


async def require_live_identity(session: AsyncSession, principal: Principal) -> None:
    """Use scalar DB values, not a potentially stale ORM identity-map instance."""
    await set_tenant_context(session, UUID(principal.tenant_id))
    row = (
        await session.execute(
            select(User.is_active, Tenant.status, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                User.id == UUID(principal.subject_id),
                Membership.tenant_id == UUID(principal.tenant_id),
            )
        )
    ).one_or_none()
    if row is None or not row[0] or row[1] != "active":
        raise AuthorizationError("Current identity is not authorized.")
    AuthorizationService().require(
        Principal(principal.subject_id, principal.tenant_id, Role(row[2])),
        Permission.READ_DOCUMENTS,
    )


class AnswerRunService:
    def __init__(
        self,
        session: AsyncSession,
        workflow: AnswerWorkflow,
        mutex: Callable[[str], AbstractAsyncContextManager[None]],
    ):
        self.session, self.workflow, self.mutex = session, workflow, mutex
        self.principal = workflow.principal
        self.tenant_id, self.user_id = (
            UUID(self.principal.tenant_id),
            UUID(self.principal.subject_id),
        )
        self.workflow.reauthorize = self.authorize

    async def authorize(self) -> None:
        await require_live_identity(self.session, self.principal)

    async def lookup(self, run_id: UUID) -> AnswerRun:
        await self.authorize()
        row = await self.session.scalar(
            select(AnswerRun)
            .where(
                AnswerRun.id == run_id,
                AnswerRun.tenant_id == self.tenant_id,
                AnswerRun.user_id == self.user_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise RunNotFoundError("Answer run is unavailable.")
        return row

    async def create(self, request: AnswerRequest) -> RunView:
        await self.authorize()
        request = AnswerRequest.model_validate(request.model_dump())
        spec = request.search(self.tenant_id)
        pack_context(tenant_id=self.tenant_id, query=spec.normalized_query, evidence=())
        run_id = uuid4()
        self.session.add(
            AnswerRun(
                id=run_id,
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                request_json=request.model_dump(mode="json"),
                provider_identity=self.workflow.generation.provider.identity,
            )
        )
        await AuditRepository(self.session).record(
            action="answer.created",
            resource_type="answer",
            resource_id=str(run_id),
            tenant_id=self.tenant_id,
            actor_user_id=self.user_id,
            outcome="success",
            metadata={"query_checksum": spec.query_checksum_sha256},
        )
        await self.session.commit()
        return await self.get(run_id)

    async def get(self, run_id: UUID) -> RunView:
        row = await self.lookup(run_id)
        answer = None
        if row.status == "complete":
            if row.answer_json is None or answer_checksum(row.answer_json) != row.answer_checksum:
                raise EvidenceIntegrityError("Stored answer checksum mismatch.")
            answer = GroundedAnswer.model_validate(row.answer_json)
            await self.workflow.validate_answer(answer)
            await self.authorize()
        return RunView(
            id=row.id,
            status=cast(RunStatus, row.status),
            attempts=row.attempts,
            error_code=row.error_code,
            answer=answer,
        )

    async def trace(self, run_id: UUID) -> dict[str, object]:
        await self.lookup(run_id)
        completed, pending = await self.workflow.inspect(run_id)
        return {
            "id": str(run_id),
            "completed_stages": list(completed),
            "next_stages": list(pending),
        }

    async def events(self, run_id: UUID) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        await self.lookup(run_id)
        key = f"answer:{self.tenant_id}:{self.user_id}:{run_id}"
        async with self.mutex(key):
            row = await self.lookup(run_id)
            if row.status == "complete":
                yield "answer", (await self.get(run_id)).model_dump(mode="json")
                return
            if row.status == "failed" or row.attempts >= 5:
                raise RunConflictError("Run exhausted its retry policy.")
            if row.provider_identity != self.workflow.generation.provider.identity:
                raise RunConflictError("Run requires its original provider configuration.")
            request = AnswerRequest.model_validate(row.request_json)
            row.status, row.attempts, row.error_code = "running", row.attempts + 1, None
            await self.session.commit()
            try:
                async with asyncio.timeout(180):
                    snapshot = await self.workflow.graph.aget_state(self.workflow.config(run_id))
                    async for stage in self.workflow.events(
                        run_id, None if snapshot.values else request
                    ):
                        yield "progress", {"id": str(run_id), "stage": stage}
                    answer = await self.workflow.result(run_id)
                    await self.authorize()
                    payload = answer.model_dump(mode="json")
                    await self.session.execute(
                        update(AnswerRun)
                        .where(
                            AnswerRun.id == run_id,
                            AnswerRun.tenant_id == self.tenant_id,
                            AnswerRun.user_id == self.user_id,
                        )
                        .values(
                            status="complete",
                            answer_json=payload,
                            answer_checksum=answer_checksum(payload),
                            error_code=None,
                        )
                    )
                    await AuditRepository(self.session).record(
                        action="answer.completed",
                        resource_type="answer",
                        resource_id=str(run_id),
                        tenant_id=self.tenant_id,
                        actor_user_id=self.user_id,
                        outcome="success",
                        metadata={"status": answer.status, "citation_count": len(answer.citations)},
                    )
                    await self.session.commit()
                    final = await self.get(run_id)
            except Exception as exc:
                await self.session.rollback()
                permanent = isinstance(
                    exc,
                    (
                        GenerationInputError,
                        GenerationOutputError,
                        EvidenceIntegrityError,
                        EvidenceNotFoundError,
                        AuthorizationError,
                    ),
                )
                # If final persistence succeeded but a release-time source check failed,
                # retain the immutable artifact; future reads still validate it.
                await set_tenant_context(self.session, self.tenant_id)
                await self.session.execute(
                    update(AnswerRun)
                    .where(
                        AnswerRun.id == run_id,
                        AnswerRun.tenant_id == self.tenant_id,
                        AnswerRun.user_id == self.user_id,
                        AnswerRun.status != "complete",
                    )
                    .values(
                        status="failed" if permanent else "retryable",
                        error_code="validation_failed" if permanent else "dependency_unavailable",
                    )
                )
                await self.session.commit()
                raise
            # Cancellation/GeneratorExit leaves durable running state; closing the
            # mutex connection releases ownership and permits checkpoint recovery.
            yield "answer", final.model_dump(mode="json")

    async def execute(self, run_id: UUID) -> RunView:
        result: RunView | None = None
        async for name, payload in self.events(run_id):
            if name == "answer":
                result = RunView.model_validate(payload)
        if result is None:
            raise RunConflictError("Run did not produce a final result.")
        return result
