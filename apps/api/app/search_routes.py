"""Authenticated search with canonical text, score explanation and safe errors."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.deduplication import EvidenceDeduplicationError
from openwikirag.application.evidence import EvidenceError
from openwikirag.application.fusion import FusionError
from openwikirag.application.graph_projection import GraphProjectionError
from openwikirag.application.reranking import RerankingError
from openwikirag.application.retrieval import (
    RetrievalError,
    RetrievalInputError,
    SearchFilters,
    SearchRequest,
)
from openwikirag.application.search import SearchResult, SearchService
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.security.authorization import AuthorizationError, Principal

from .dependencies import get_current_principal, get_session
from .search_dependencies import get_search_service

router = APIRouter(prefix="/api/v1", tags=["search"])


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4096)
    mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    candidate_limit: int = Field(default=20, ge=1, le=100, strict=True)
    limit: int = Field(default=10, ge=1, le=40, strict=True)
    rerank: bool = Field(default=False, strict=True)
    graph_hops: int = Field(default=0, ge=0, le=2, strict=True)
    document_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    document_version_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    source_types: tuple[str, ...] = Field(default=(), max_length=50)
    languages: tuple[str, ...] = Field(default=(), max_length=50)
    chunk_kinds: tuple[Literal["parent", "child"], ...] = Field(default=(), max_length=50)
    pipeline_versions: tuple[str, ...] = Field(default=(), max_length=50)

    def to_request(self, tenant_id: UUID) -> SearchRequest:
        return SearchRequest(
            tenant_id=tenant_id,
            query=self.query,
            mode=self.mode,
            candidate_limit=self.candidate_limit,
            filters=SearchFilters(
                **self.model_dump(
                    exclude={
                        "query",
                        "mode",
                        "candidate_limit",
                        "limit",
                        "rerank",
                        "graph_hops",
                    }
                )
            ),
        )


@router.post("/search", response_model=SearchResult)
async def search(
    body: SearchBody,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[SearchService, Depends(get_search_service)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SearchResult:
    try:
        result = await service.search(
            principal=principal,
            request=body.to_request(UUID(principal.tenant_id)),
            limit=body.limit,
            rerank=body.rerank,
            graph_hops=body.graph_hops,
        )
        await AuditRepository(session).record(
            action="search.read",
            resource_type="search",
            outcome="success",
            tenant_id=UUID(principal.tenant_id),
            actor_user_id=UUID(principal.subject_id),
            request_id=request.headers.get("X-Request-ID"),
            metadata={"query_checksum": result.query_checksum, "hit_count": len(result.hits)},
        )
        await session.commit()
        return result
    except RetrievalInputError as exc:
        raise HTTPException(422, "Invalid search query or filters.") from exc
    except AuthorizationError as exc:
        raise HTTPException(403, "Search is not authorized.") from exc
    except (
        RetrievalError,
        EvidenceError,
        FusionError,
        EvidenceDeduplicationError,
        RerankingError,
        GraphProjectionError,
        SQLAlchemyError,
    ) as exc:
        await session.rollback()
        raise HTTPException(503, "Search evidence is temporarily unavailable.") from exc
