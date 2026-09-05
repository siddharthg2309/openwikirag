from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag import __version__
from openwikirag.core.config import get_settings
from openwikirag.core.logging import configure_logging
from openwikirag.infrastructure.repositories.audit import AuditRepository
from openwikirag.security.authorization import Principal, Role

from .answer_routes import router as answer_router
from .auth_routes import router as auth_router
from .dependencies import get_current_principal, get_session
from .document_routes import router as document_router
from .job_routes import router as job_router
from .search_routes import router as search_router
from .wiki_routes import router as wiki_router

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="OpenWikiRAG API",
    version=__version__,
    description="Enterprise knowledge platform API.",
)
app.include_router(auth_router)
app.include_router(document_router)
app.include_router(job_router)
app.include_router(wiki_router)
app.include_router(search_router)
app.include_router(answer_router)


class MeResponse(BaseModel):
    """The authenticated identity for the explicitly selected tenant."""

    subject_id: str
    tenant_id: str
    role: Role


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {"service": "openwikirag-api", "version": __version__}


@app.get("/healthz", tags=["operations"])
async def healthz() -> dict[str, str]:
    """Liveness: this process is running and can serve HTTP."""

    return {"status": "ok"}


@app.get("/readyz", tags=["operations"])
async def readyz() -> dict[str, object]:
    """Phase 0 readiness: configuration is valid.

    Dependency checks will be added with the first persistence phase. Keeping
    this distinction explicit avoids reporting a false dependency guarantee.
    """

    return {"status": "ready", "checks": {"configuration": "ok"}}


@app.get("/api/v1/me", response_model=MeResponse, tags=["identity"])
async def me(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    """Return the database-backed identity context for the current request."""

    await AuditRepository(session).record(
        action="identity.read",
        resource_type="user",
        resource_id=principal.subject_id,
        tenant_id=UUID(principal.tenant_id),
        actor_user_id=UUID(principal.subject_id),
        request_id=request.headers.get("X-Request-ID"),
        outcome="success",
    )
    await session.commit()

    return MeResponse(
        subject_id=principal.subject_id,
        tenant_id=principal.tenant_id,
        role=principal.role,
    )
