"""Read-only retrieval adapters; lazy optional process-local reranker."""

from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.evidence import CanonicalEvidenceResolver
from openwikirag.application.reranking import RerankingService
from openwikirag.application.retrieval import CandidateRetrievalService
from openwikirag.application.search import SearchService
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.qdrant import QdrantVectorIndex
from openwikirag.infrastructure.reranking import SentenceTransformersReranker
from openwikirag.infrastructure.storage import ObjectStorage
from openwikirag.security.authorization import Principal

from .dependencies import get_current_principal, get_object_storage, get_session


@lru_cache(maxsize=1)
def get_reranker() -> RerankingService | None:
    settings = get_settings()
    if not settings.reranker_model:
        return None
    return RerankingService(
        SentenceTransformersReranker(
            model=settings.reranker_model,
            revision=settings.reranker_revision,
        )
    )


async def get_search_service(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> AsyncGenerator[SearchService]:
    # The SDK's compatibility probe is synchronous even on AsyncQdrantClient.
    index = QdrantVectorIndex.from_settings(get_settings(), check_compatibility=False)
    try:
        yield SearchService(
            CandidateRetrievalService(index),
            CanonicalEvidenceResolver(session, storage),
            get_reranker(),
        )
    finally:
        await index.close()
