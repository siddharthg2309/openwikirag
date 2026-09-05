"""Request-owned answer composition; all external clients close deterministically."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.answer_runs import AnswerRunService
from openwikirag.application.answers import GroundedGenerationService
from openwikirag.application.search import SearchService
from openwikirag.application.source_evidence import SourceEvidenceResolver
from openwikirag.application.workflow import AnswerWorkflow
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.checkpoints import checkpoint_store
from openwikirag.infrastructure.ollama import OllamaAnswerProvider
from openwikirag.infrastructure.run_mutex import PostgresRunMutex
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import Principal

from .dependencies import get_current_principal, get_object_storage, get_session
from .search_dependencies import get_search_service


async def get_answer_run_service(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    search: Annotated[SearchService, Depends(get_search_service)],
) -> AsyncGenerator[AnswerRunService]:
    settings = get_settings()
    if not settings.answer_model:
        raise HTTPException(503, "Answer generation is not configured.")
    provider = OllamaAnswerProvider(
        model=settings.answer_model,
        digest=settings.answer_model_digest,
        base_url=settings.answer_base_url,
    )
    async with checkpoint_store(settings.database_url) as saver:
        workflow = AnswerWorkflow(
            principal=principal,
            search=search,
            resolver=lambda: SourceEvidenceResolver(session, storage),
            generation=GroundedGenerationService(provider),
            checkpointer=saver,
        )
        yield AnswerRunService(session, workflow, PostgresRunMutex(settings.database_url).hold)
