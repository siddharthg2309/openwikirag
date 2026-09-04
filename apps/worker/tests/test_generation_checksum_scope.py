"""Identical content must not collide across tenant-owned generations."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.tests.test_ingestion import (
    FakeTransport,
    create_job_with_source,
    make_message,
    make_service,
)
from apps.api.tests.test_ingestion import session as session
from openwikirag.application.vector_index import InMemoryVectorIndex
from openwikirag.application.wiki_ingestion import WikiIngestionHandler
from openwikirag.infrastructure.models import WikiGenerationArtifact
from openwikirag.infrastructure.storage import LocalObjectStorage


async def test_identical_generation_content_can_belong_to_two_tenants(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    index = InMemoryVectorIndex()
    handler = WikiIngestionHandler(session, storage, index, config_hash="b" * 64)
    for _ in range(2):
        tenant, job = await create_job_with_source(
            session,
            storage,
            source_type="markdown",
            data=b"# Shared\nIdentical source bytes.\n",
        )
        transport = FakeTransport()
        transport.new_messages.append(make_message(tenant.id, job.id))
        assert await make_service(session, transport, handler).consume_once() == 1
        await session.refresh(job)
        assert job.status == "succeeded"
    rows = tuple(await session.scalars(select(WikiGenerationArtifact)))
    assert len(rows) == 2 and rows[0].tenant_id != rows[1].tenant_id
    assert rows[0].result_checksum_sha256 == rows[1].result_checksum_sha256
    assert rows[0].artifact_object_key != rows[1].artifact_object_key
