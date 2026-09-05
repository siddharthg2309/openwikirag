"""Owner-scoped answer artifacts, recovery controls and validated SSE."""

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from openwikirag.application.answer_runs import (
    AnswerRunService,
    RunBusyError,
    RunConflictError,
    RunNotFoundError,
    RunView,
)
from openwikirag.application.answers import (
    GenerationError,
    GenerationInputError,
    GenerationOutputError,
)
from openwikirag.application.evidence import (
    EvidenceError,
    EvidenceIntegrityError,
    EvidenceNotFoundError,
)
from openwikirag.application.workflow import AnswerRequest
from openwikirag.security.authorization import AuthorizationError

from .answer_dependencies import get_answer_run_service

router = APIRouter(prefix="/api/v1/answers", tags=["answers"])


def mapped(exc: Exception) -> HTTPException:
    if isinstance(exc, RunNotFoundError):
        return HTTPException(404, "Answer run not found.")
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, "Answer access is not authorized.")
    if isinstance(exc, (RunBusyError, RunConflictError)):
        return HTTPException(409, "Answer run cannot execute in its current state.")
    return HTTPException(503, "Answer generation is temporarily unavailable.")


@router.post("", response_model=RunView, status_code=202)
async def create_answer(
    body: AnswerRequest, service: Annotated[AnswerRunService, Depends(get_answer_run_service)]
) -> RunView:
    try:
        return await service.create(body)
    except (GenerationError, EvidenceError, AuthorizationError, SQLAlchemyError) as exc:
        raise mapped(exc) from exc


@router.get("/{run_id}", response_model=RunView)
async def get_answer(
    run_id: UUID, service: Annotated[AnswerRunService, Depends(get_answer_run_service)]
) -> RunView:
    try:
        return await service.get(run_id)
    except (
        RunNotFoundError,
        GenerationError,
        EvidenceError,
        AuthorizationError,
        SQLAlchemyError,
    ) as exc:
        raise mapped(exc) from exc


@router.get("/{run_id}/trace")
async def get_trace(
    run_id: UUID, service: Annotated[AnswerRunService, Depends(get_answer_run_service)]
) -> dict[str, object]:
    try:
        return await service.trace(run_id)
    except (RunNotFoundError, AuthorizationError, SQLAlchemyError) as exc:
        raise mapped(exc) from exc


@router.post("/{run_id}/execute", response_model=RunView)
async def execute_answer(
    run_id: UUID, service: Annotated[AnswerRunService, Depends(get_answer_run_service)]
) -> RunView:
    try:
        return await service.execute(run_id)
    except Exception as exc:
        raise mapped(exc) from exc


def sse(name: str, payload: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


@router.post("/{run_id}/stream")
async def stream_answer(
    run_id: UUID, service: Annotated[AnswerRunService, Depends(get_answer_run_service)]
) -> StreamingResponse:
    try:
        await service.lookup(run_id)
    except (RunNotFoundError, AuthorizationError, SQLAlchemyError) as exc:
        raise mapped(exc) from exc

    async def body() -> AsyncIterator[bytes]:
        try:
            async for name, payload in service.events(run_id):
                yield sse(name, payload)
        except Exception as exc:
            status: str | None = None
            try:
                status = (await service.get(run_id)).status
                retryable = status == "retryable"
            except Exception:
                retryable = False
            if status != "failed" and not isinstance(
                exc,
                (
                    RunConflictError,
                    RunNotFoundError,
                    GenerationInputError,
                    GenerationOutputError,
                    EvidenceIntegrityError,
                    EvidenceNotFoundError,
                    AuthorizationError,
                ),
            ):
                retryable = True
            yield sse("error", {"id": str(run_id), "retryable": retryable})

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
