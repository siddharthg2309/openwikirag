"""Async tenant-keyed Neo4j projection; canonical evidence remains PostgreSQL-owned."""

import re
from uuid import UUID

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction, Query, unit_of_work

from openwikirag.application.graph_projection import GraphNeighbor, GraphProjectionError
from openwikirag.application.knowledge import KnowledgeArtifact
from openwikirag.core.config import Settings


class Neo4jProjection:
    def __init__(self, driver: AsyncDriver, *, database: str = "neo4j") -> None:
        self.driver, self.database = driver, database

    @classmethod
    def from_settings(cls, settings: Settings) -> "Neo4jProjection":
        return cls(
            AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
                connection_timeout=5,
                max_transaction_retry_time=5,
                max_connection_pool_size=8,
            ),
            database=settings.neo4j_database,
        )

    async def ensure_schema(self) -> None:
        for query in (
            "CREATE CONSTRAINT ow_entity_identity IF NOT EXISTS FOR (n:OWEntity) "
            "REQUIRE (n.tenant_id, n.id) IS UNIQUE",
            "CREATE CONSTRAINT ow_fact_identity IF NOT EXISTS FOR (n:OWFact) "
            "REQUIRE (n.tenant_id, n.id) IS UNIQUE",
            "CREATE CONSTRAINT ow_artifact_identity IF NOT EXISTS FOR (n:OWArtifact) "
            "REQUIRE (n.tenant_id, n.id) IS UNIQUE",
        ):
            await self.driver.execute_query(Query(query, timeout=15), database_=self.database)

    async def upsert(self, artifact: KnowledgeArtifact) -> None:
        try:
            artifact = KnowledgeArtifact.model_validate(artifact.model_dump())
            async with self.driver.session(database=self.database) as session:
                await session.execute_write(self._write_artifact, artifact)
        except Exception as exc:
            raise GraphProjectionError("Graph projection write failed.") from exc

    @staticmethod
    @unit_of_work(timeout=10)
    async def _write_artifact(tx: AsyncManagedTransaction, artifact: KnowledgeArtifact) -> None:
        parameters = {"tenant": str(artifact.tenant_id), "artifact": str(artifact.id)}
        result = await tx.run(
            "MERGE (a:OWArtifact {tenant_id:$tenant, id:$artifact}) "
            "ON CREATE SET a.checksum=$checksum "
            "WITH a WHERE a.checksum=$checksum RETURN a.id AS id",
            parameters={**parameters, "checksum": artifact.checksum},
        )
        if await result.single() is None:
            raise ValueError("Graph artifact immutable checksum conflict.")
        await (
            await tx.run(
                "UNWIND $entities AS item "
                "MERGE (e:OWEntity {tenant_id:$tenant, id:item.id}) "
                "ON CREATE SET e.name=item.name",
                tenant=str(artifact.tenant_id),
                entities=[entity.model_dump() for entity in artifact.entities],
            )
        ).consume()
        await (
            await tx.run(
                "UNWIND $facts AS item "
                "MATCH (s:OWEntity {tenant_id:$tenant, id:item.subject_id}) "
                "MATCH (o:OWEntity {tenant_id:$tenant, id:item.object_id}) "
                "MERGE (f:OWFact {tenant_id:$tenant, id:item.id}) "
                "ON CREATE SET f.artifact_id=$artifact, f.subject_id=item.subject_id, "
                "f.object_id=item.object_id, f.predicate=item.predicate "
                "MERGE (s)-[:OW_SUBJECT]->(f) MERGE (f)-[:OW_OBJECT]->(o)",
                parameters={**parameters, "facts": [fact.model_dump() for fact in artifact.facts]},
            )
        ).consume()

    async def neighbors(
        self,
        *,
        tenant_id: UUID,
        entity_id: str,
        limit: int = 20,
    ) -> tuple[GraphNeighbor, ...]:
        if (
            not isinstance(tenant_id, UUID)
            or not re.fullmatch(r"[0-9a-f]{64}", entity_id)
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise GraphProjectionError("Invalid graph read controls.")
        try:
            records, _, _ = await self.driver.execute_query(
                Query(
                    "MATCH (s:OWEntity {tenant_id:$tenant, id:$entity})"
                    "-[:OW_SUBJECT|OW_OBJECT]-(f:OWFact {tenant_id:$tenant}) "
                    "RETURN DISTINCT f.tenant_id AS tenant_id, f.artifact_id AS artifact_id, "
                    "f.id AS fact_id, f.subject_id AS subject_id, f.object_id AS object_id "
                    "ORDER BY fact_id LIMIT $limit",
                    timeout=3,
                ),
                tenant=str(tenant_id),
                entity=entity_id,
                limit=limit,
                database_=self.database,
                routing_="r",
            )
            result = tuple(GraphNeighbor.model_validate(dict(row)) for row in records)
            if len(result) > limit or any(item.tenant_id != tenant_id for item in result):
                raise ValueError("Foreign or oversized graph result.")
            return result
        except Exception as exc:
            raise GraphProjectionError("Graph projection read failed.") from exc

    async def clear(self, *, tenant_id: UUID, confirmed_tenant: UUID) -> None:
        if not isinstance(tenant_id, UUID) or tenant_id != confirmed_tenant:
            raise GraphProjectionError("Tenant clear requires an exact confirmation.")
        # Small deletion batches bound transaction size; only application-owned labels.
        while True:
            records, _, _ = await self.driver.execute_query(
                Query(
                    "MATCH (n {tenant_id:$tenant}) WHERE n:OWEntity OR n:OWFact OR n:OWArtifact "
                    "WITH n LIMIT 500 DETACH DELETE n RETURN count(n) AS deleted",
                    timeout=10,
                ),
                tenant=str(tenant_id),
                database_=self.database,
            )
            if not records or int(records[0]["deleted"]) == 0:
                break

    async def close(self) -> None:
        await self.driver.close()
