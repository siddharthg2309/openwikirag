"""Replay tenant vectors: python -m openwikirag.vector_cli --tenant UUID."""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from openwikirag.application.vector_ingestion import VectorIngestionConfig
from openwikirag.application.vector_rebuild import rebuild_vector_projection
from openwikirag.core.config import get_settings
from openwikirag.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    set_tenant_context,
)
from openwikirag.infrastructure.qdrant import QdrantCollectionConfig, QdrantVectorIndex
from openwikirag.infrastructure.storage import LocalObjectStorage


async def run(tenant_id: UUID) -> int:
    """Replay one tenant's canonical normalized artifacts into Qdrant."""

    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    vector_config = VectorIngestionConfig()
    index = QdrantVectorIndex.from_settings(
        settings,
        config=QdrantCollectionConfig(vector=vector_config.collection),
    )
    storage = LocalObjectStorage(Path(settings.object_store_root))
    try:
        await index.ensure_schema()
        async with create_session_factory(engine)() as session:
            await set_tenant_context(session, tenant_id)
            result = await rebuild_vector_projection(
                tenant_id=tenant_id,
                session=session,
                storage=storage,
                vector_index=index,
                config=vector_config,
            )
        print(
            "Replayed "
            f"{result.normalized_artifacts} normalized artifacts, "
            f"created {result.points_created} points, "
            f"reused {result.points_reused} points."
        )
        return result.normalized_artifacts
    finally:
        await index.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=UUID, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.tenant))


if __name__ == "__main__":
    main()
