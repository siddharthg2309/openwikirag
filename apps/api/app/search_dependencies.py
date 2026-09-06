"""Read-only retrieval adapters; lazy optional process-local reranker."""

from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.evidence import CanonicalEvidenceResolver
from openwikirag.application.graph_retrieval import GraphExpansionService
from openwikirag.application.reranking import RerankingService
from openwikirag.application.retrieval import CandidateRetrievalConfig, CandidateRetrievalService
from openwikirag.application.search import SearchService
from openwikirag.application.vector_index import VectorCollectionConfig
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.embedding_factory import (
    build_dense_embedding_config,
    build_dense_embedding_provider,
)
from openwikirag.infrastructure.neo4j import Neo4jProjection
from openwikirag.infrastructure.qdrant import QdrantCollectionConfig, QdrantVectorIndex
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
    settings = get_settings()
    dense_config = build_dense_embedding_config(settings)
    collection_config = VectorCollectionConfig(dense_dimensions=dense_config.dimensions)
    # The SDK's compatibility probe is synchronous even on AsyncQdrantClient.
    index = QdrantVectorIndex.from_settings(
        settings,
        config=QdrantCollectionConfig(vector=collection_config),
        check_compatibility=False,
    )
    graph = Neo4jProjection.from_settings(settings) if settings.graph_enabled else None
    try:
        yield SearchService(
            CandidateRetrievalService(
                index,
                config=CandidateRetrievalConfig(
                    dense=dense_config,
                    collection=collection_config,
                ),
                dense_provider=build_dense_embedding_provider(settings),
            ),
            CanonicalEvidenceResolver(session, storage),
            get_reranker(),
            GraphExpansionService(session, storage, graph) if graph else None,
        )
    finally:
        try:
            if graph:
                await graph.close()
        finally:
            await index.close()
