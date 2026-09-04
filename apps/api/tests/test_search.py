"""Composed search proof against SQLite, immutable objects and ranked vectors."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.dependencies import get_session
from apps.api.app.main import app
from apps.api.app.search_dependencies import get_search_service
from apps.api.tests.test_jobs import make_token
from apps.worker.tests.test_chunk_artifacts import ChunkArtifactContext
from apps.worker.tests.test_chunk_artifacts import chunk_artifact_context as chunk_artifact_context
from apps.worker.tests.test_evidence import _persist
from apps.worker.tests.test_reranking import FakeReranker
from apps.worker.tests.test_retrieval import _config
from apps.worker.tests.test_vector_index import _request
from openwikirag.application.evidence import CanonicalEvidenceResolver
from openwikirag.application.reranking import RerankingService
from openwikirag.application.retrieval import (
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    SearchRequest,
)
from openwikirag.application.search import SearchService
from openwikirag.infrastructure.models import AuditEvent, Document, DocumentVersion, IngestionJob
from openwikirag.infrastructure.repositories.identity import IdentityRepository
from openwikirag.security.authorization import AuthorizationError, Principal, Role


class SearchContext:
    def __init__(self, ctx: ChunkArtifactContext, service: SearchService, principal: Principal):
        self.ctx, self.service, self.principal = ctx, service, principal


@pytest.fixture
async def search_context(
    chunk_artifact_context: ChunkArtifactContext,
) -> AsyncIterator[SearchContext]:
    ctx = chunk_artifact_context
    _, payload = await _persist(ctx)
    point = (await _request(text=ctx.result.chunks[0].text)).build_point()
    # Only canonical source provenance differs from the small-geometry fixture.
    point = point.model_copy(
        update={
            "payload": payload.model_copy(
                update={
                    "sparse_token_count": point.payload.sparse_token_count,
                }
            )
        }
    )
    user = await IdentityRepository(ctx.session).create_user(
        "search@example.com",
        auth_provider_subject="oidc|search",
    )
    await IdentityRepository(ctx.session).add_membership(ctx.tenant_id, user.id, Role.VIEWER)
    ctx.session.add(
        IngestionJob(
            tenant_id=ctx.tenant_id,
            document_version_id=ctx.document_version_id,
            status="succeeded",
        )
    )
    await ctx.session.commit()
    principal = Principal(str(user.id), str(ctx.tenant_id), Role.VIEWER)
    service = SearchService(
        CandidateRetrievalService(InMemoryCandidateIndex((point,)), config=_config()),
        CanonicalEvidenceResolver(ctx.session, ctx.storage),
    )

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield ctx.session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_search_service] = lambda: service
    try:
        yield SearchContext(ctx, service, principal)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_search_service, None)


async def _post(
    context: SearchContext,
    body: dict[str, object],
    token: str | None = "valid",
) -> Response:
    headers = (
        {}
        if token is None
        else {
            "Authorization": "Bearer "
            + (make_token("oidc|search", context.ctx.tenant_id) if token == "valid" else token)
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/search", json=body, headers=headers)


async def test_search_returns_canonical_text_explanations_and_private_audit(
    search_context: SearchContext,
) -> None:
    ctx = search_context
    response = await _post(ctx, {"query": "stable citations"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["hits"]) == 1
    assert data["hits"][0]["evidence"]["chunk"]["text"] == ctx.ctx.result.chunks[0].text
    assert data["hits"][0]["explanation"]["representative"]["contributions"]
    assert "object_key" not in response.text
    audit = await ctx.ctx.session.scalar(
        select(AuditEvent).where(AuditEvent.action == "search.read")
    )
    assert audit is not None and "stable citations" not in str(audit.metadata_json)


@pytest.mark.parametrize(
    "body",
    [
        {"query": "test", "tenant_id": str(uuid4())},
        {"query": "test", "limit": True},
        {"query": "\x00"},
        {"query": "test", "candidate_limit": 101},
        {"query": "test", "source_types": ["text", "text"]},
    ],
)
async def test_invalid_search_controls_rejected(
    search_context: SearchContext,
    body: dict[str, object],
) -> None:
    assert (await _post(search_context, body)).status_code == 422


async def test_auth_and_filters_fail_closed(search_context: SearchContext) -> None:
    ctx = search_context
    assert (await _post(ctx, {"query": "test"}, token=None)).status_code == 401
    foreign = make_token("oidc|search", ctx.ctx.foreign_tenant_id)
    assert (await _post(ctx, {"query": "test"}, token=foreign)).status_code == 403
    result = await _post(ctx, {"query": "test", "document_ids": [str(uuid4())]})
    assert result.status_code == 200 and not result.json()["hits"]


async def test_current_version_and_successful_ingestion_are_required(
    search_context: SearchContext,
) -> None:
    ctx = search_context
    job = await ctx.ctx.session.scalar(select(IngestionJob))
    assert job is not None
    job.status = "processing"
    await ctx.ctx.session.commit()
    assert not (await _post(ctx, {"query": "test"})).json()["hits"]
    job.status = "succeeded"
    version = await ctx.ctx.session.get(DocumentVersion, ctx.ctx.document_version_id)
    assert version is not None
    document = await ctx.ctx.session.get(Document, version.document_id)
    assert document is not None
    document.current_version_id = None
    await ctx.ctx.session.commit()
    assert not (await _post(ctx, {"query": "test"})).json()["hits"]


async def test_explicit_reranking_and_dependency_errors(
    search_context: SearchContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = search_context
    assert (await _post(ctx, {"query": "test", "rerank": True})).status_code == 503
    ctx.service.reranker = RerankingService(FakeReranker((0.8,)))
    response = await _post(ctx, {"query": "test", "rerank": True})
    assert response.status_code == 200, response.text
    assert response.json()["hits"][0]["rerank_score"] == 0.8
    monkeypatch.setattr(ctx.ctx.storage, "get", AsyncMock(return_value=b"corrupted"))
    # A fresh request resolver is required; production constructs one per request.
    ctx.service.resolver = CanonicalEvidenceResolver(ctx.ctx.session, ctx.ctx.storage)
    response = await _post(ctx, {"query": "test"})
    assert response.status_code == 503 and "corrupted" not in response.text


async def test_inactive_principal_is_rejected_before_index(
    search_context: SearchContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = search_context
    retrieve = AsyncMock()
    monkeypatch.setattr(ctx.service.retrieval, "retrieve", retrieve)
    with pytest.raises(AuthorizationError):
        await ctx.service.search(
            principal=Principal(
                ctx.principal.subject_id, ctx.principal.tenant_id, Role.VIEWER, False
            ),
            request=SearchRequest(tenant_id=ctx.ctx.tenant_id, query="test"),
        )
    retrieve.assert_not_awaited()


async def test_request_dependency_disables_sync_probe_and_closes_client(
    search_context: SearchContext,
) -> None:
    ctx = search_context
    client = AsyncMock()
    with patch(
        "openwikirag.infrastructure.qdrant.AsyncQdrantClient", return_value=client
    ) as factory:
        generator = get_search_service(ctx.principal, ctx.ctx.session, ctx.ctx.storage)
        assert isinstance(await anext(generator), SearchService)
        factory.assert_called_once()
        assert factory.call_args.kwargs["check_compatibility"] is False
        client.get_collections.assert_not_called()
        await generator.aclose()
        client.close.assert_awaited_once()
