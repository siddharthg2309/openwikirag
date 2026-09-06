"""Bounded BFS whose projected edges must survive canonical evidence validation."""

import asyncio
import hashlib
import re
import unicodedata
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.evidence import (
    EvidenceDependencyError,
    EvidenceIntegrityError,
    EvidenceNotFoundError,
)
from openwikirag.application.extraction import InvalidProvenanceError, read_normalized_document
from openwikirag.application.graph_projection import (
    GraphNeighbor,
    GraphProjection,
    GraphProjectionError,
)
from openwikirag.application.knowledge import KnowledgeArtifact, RelationFact
from openwikirag.application.metadata import DeterministicMetadataExtractor, MetadataExtractionError
from openwikirag.application.retrieval import SearchFilters
from openwikirag.application.source_evidence import SourceEvidence, SourceEvidenceResolver
from openwikirag.infrastructure.models import (
    ChunkManifestArtifact,
    DocumentVersion,
    KnowledgeArtifactRow,
    NormalizedDocumentArtifact,
)
from openwikirag.infrastructure.storage import ObjectStorage, ObjectStorageError
from openwikirag.security.authorization import AuthorizationService, Permission, Principal


class GraphEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence: SourceEvidence
    fact: RelationFact
    hop: int = Field(ge=1, le=2)


class GraphExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence: tuple[GraphEvidence, ...]
    seed_count: int
    visited_count: int
    stale_count: int


def _artifact(row: KnowledgeArtifactRow, tenant_id: UUID) -> KnowledgeArtifact:
    try:
        artifact = KnowledgeArtifact.model_validate(row.payload)
    except ValueError as exc:
        raise EvidenceIntegrityError("Canonical graph artifact schema is invalid.") from exc
    if (
        artifact.tenant_id != tenant_id
        or artifact.id != row.id
        or artifact.checksum != row.checksum
        or artifact.manifest_id != row.manifest_id
        or artifact.document_version_id != row.document_version_id
    ):
        raise EvidenceIntegrityError("Canonical graph artifact identity mismatch.")
    return artifact


class GraphExpansionService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage, projection: GraphProjection):
        self.session, self.storage, self.projection = session, storage, projection
        self.resolver = SourceEvidenceResolver(session, storage)
        self._language_cache: dict[tuple[UUID, UUID], str] = {}

    async def expand(
        self,
        *,
        principal: Principal,
        seeds: tuple[SourceEvidence, ...],
        hops: int = 2,
        filters: SearchFilters | None = None,
    ) -> GraphExpansion:
        AuthorizationService().require(principal, Permission.READ_DOCUMENTS)
        tenant_id = UUID(principal.tenant_id)
        filters = filters or SearchFilters()
        if not isinstance(filters, SearchFilters):
            raise GraphProjectionError("Graph filters are invalid.")
        if type(hops) is not int or hops not in (1, 2) or len(seeds) > 40:
            raise GraphProjectionError("Invalid graph expansion bounds.")
        seeds = tuple(SourceEvidence.model_validate(seed.model_dump()) for seed in seeds)
        if any(seed.tenant_id != tenant_id for seed in seeds):
            raise GraphProjectionError("Foreign graph seed.")
        if any(len(seed.chunk.text) > 32_000 for seed in seeds):
            raise GraphProjectionError("Graph seed text exceeds its matching budget.")
        try:
            async with asyncio.timeout(10):
                return await self._expand(tenant_id, seeds, hops, filters)
        except TimeoutError as exc:
            raise GraphProjectionError("Graph expansion timed out.") from exc

    async def _expand(
        self,
        tenant_id: UUID,
        seeds: tuple[SourceEvidence, ...],
        hops: int,
        filters: SearchFilters,
    ) -> GraphExpansion:
        frontier: set[str] = set()
        for seed in seeds:
            try:
                canonical = await self.resolver.resolve(
                    tenant_id=tenant_id,
                    manifest_id=seed.manifest_id,
                    chunk_id=seed.chunk.chunk_id,
                )
            except EvidenceNotFoundError:
                continue
            if canonical != seed:
                raise EvidenceIntegrityError("Graph seed differs from its canonical source.")
            rows = tuple(
                await self.session.scalars(
                    select(KnowledgeArtifactRow)
                    .where(
                        KnowledgeArtifactRow.tenant_id == tenant_id,
                        KnowledgeArtifactRow.manifest_id == seed.manifest_id,
                    )
                    .order_by(KnowledgeArtifactRow.id)
                    .limit(9)
                )
            )
            if len(rows) > 8:
                raise EvidenceIntegrityError("Too many graph variants.")
            text = unicodedata.normalize("NFKC", seed.chunk.text).casefold()
            for row in rows:
                for entity in _artifact(row, tenant_id).entities:
                    if re.search(r"(?<!\w)" + re.escape(entity.name) + r"(?!\w)", text):
                        frontier.add(entity.id)
        frontier = set(sorted(frontier)[:10])
        seed_count, stale = len(frontier), 0
        visited: set[str] = set()
        seen_facts: set[str] = set()
        passages: dict[str, GraphEvidence] = {}
        for hop in range(1, hops + 1):
            next_frontier: set[str] = set()
            for identity in sorted(frontier - visited):
                if len(visited) >= 20:
                    break
                visited.add(identity)
                neighbors = await self.projection.neighbors(
                    tenant_id=tenant_id, entity_id=identity, limit=10
                )
                if len(neighbors) > 10:
                    raise GraphProjectionError("Graph returned too many neighbors.")
                for candidate in neighbors:
                    candidate = GraphNeighbor.model_validate(candidate.model_dump())
                    if candidate.tenant_id != tenant_id or identity not in (
                        candidate.subject_id,
                        candidate.object_id,
                    ):
                        raise GraphProjectionError("Graph returned a foreign or disconnected fact.")
                    if candidate.fact_id in seen_facts:
                        continue
                    seen_facts.add(candidate.fact_id)
                    try:
                        resolved = await self._resolve(tenant_id, candidate, filters=filters)
                    except EvidenceNotFoundError:
                        stale += 1
                        continue
                    if resolved is None:
                        continue
                    evidence, fact = resolved
                    passages.setdefault(
                        evidence.identity, GraphEvidence(evidence=evidence, fact=fact, hop=hop)
                    )
                    next_frontier.update((fact.subject_id, fact.object_id))
                    if len(passages) >= 20:
                        return GraphExpansion(
                            evidence=tuple(passages.values()),
                            seed_count=seed_count,
                            visited_count=len(visited),
                            stale_count=stale,
                        )
            frontier = set(sorted(next_frontier - visited)[:20])
        return GraphExpansion(
            evidence=tuple(passages.values()),
            seed_count=seed_count,
            visited_count=len(visited),
            stale_count=stale,
        )

    async def _resolve(
        self,
        tenant_id: UUID,
        ref: GraphNeighbor,
        *,
        filters: SearchFilters,
    ) -> tuple[SourceEvidence, RelationFact] | None:
        row = await self.resolver.repository.get(tenant_id=tenant_id, artifact_id=ref.artifact_id)
        if row is None:
            raise EvidenceNotFoundError("Graph artifact unavailable.")
        artifact = _artifact(row, tenant_id)
        fact = next((fact for fact in artifact.facts if fact.id == ref.fact_id), None)
        if fact is None or (fact.subject_id, fact.object_id) != (ref.subject_id, ref.object_id):
            raise EvidenceIntegrityError("Graph fact differs from its canonical assertion.")
        source = await self.resolver.repository.source(
            tenant_id=tenant_id, manifest_id=artifact.manifest_id, current_ready=True
        )
        if source is None:
            raise EvidenceNotFoundError("Graph evidence is stale.")
        if source[0].manifest_checksum_sha256 != artifact.manifest_checksum:
            raise EvidenceIntegrityError("Graph source checksum mismatch.")
        evidence = await self.resolver.resolve(
            tenant_id=tenant_id, manifest_id=artifact.manifest_id, chunk_id=fact.chunk_id
        )
        start = fact.start_char - evidence.chunk.normalized_start_char
        if (
            evidence.document_version_id != artifact.document_version_id
            or start < 0
            or evidence.chunk.text[start : start + len(fact.quote)] != fact.quote
        ):
            raise EvidenceIntegrityError("Graph quotation is unsupported by source text.")
        if not await self._matches_filters(
            tenant_id=tenant_id,
            source=source,
            evidence=evidence,
            filters=filters,
        ):
            return None
        return evidence, fact

    async def _matches_filters(
        self,
        *,
        tenant_id: UUID,
        source: tuple[ChunkManifestArtifact, DocumentVersion],
        evidence: SourceEvidence,
        filters: SearchFilters,
    ) -> bool:
        """Apply retrieval filters using canonical version and normalized metadata."""

        manifest_row, version = source
        if filters.document_ids and version.document_id not in filters.document_ids:
            return False
        if filters.document_version_ids and version.id not in filters.document_version_ids:
            return False
        if filters.source_types and version.source_type not in filters.source_types:
            return False
        if filters.pipeline_versions and version.pipeline_version not in filters.pipeline_versions:
            return False
        if filters.chunk_kinds and evidence.chunk.chunk_kind not in filters.chunk_kinds:
            return False
        if not filters.languages:
            return True
        language = await self._canonical_language(
            tenant_id=tenant_id,
            normalized_artifact_id=manifest_row.normalized_artifact_id,
            document_version_id=version.id,
            source_checksum=manifest_row.source_artifact_checksum,
        )
        return language in filters.languages

    async def _canonical_language(
        self,
        *,
        tenant_id: UUID,
        normalized_artifact_id: UUID,
        document_version_id: UUID,
        source_checksum: str,
    ) -> str:
        """Read and verify canonical normalized text before deriving its language."""

        cache_key = (tenant_id, normalized_artifact_id)
        cached = self._language_cache.get(cache_key)
        if cached is not None:
            return cached
        row = await self.session.scalar(
            select(NormalizedDocumentArtifact).where(
                NormalizedDocumentArtifact.id == normalized_artifact_id,
                NormalizedDocumentArtifact.tenant_id == tenant_id,
                NormalizedDocumentArtifact.document_version_id == document_version_id,
                NormalizedDocumentArtifact.content_checksum_sha256 == source_checksum,
            )
        )
        if row is None:
            raise EvidenceIntegrityError("Graph language metadata source is unavailable.")
        try:
            data = await self.storage.get(object_key=row.artifact_object_key)
        except ObjectStorageError as exc:
            raise EvidenceDependencyError("Graph language metadata dependency failed.") from exc
        if hashlib.sha256(data).hexdigest() != row.content_checksum_sha256:
            raise EvidenceIntegrityError("Graph language metadata checksum mismatch.")
        try:
            normalized = read_normalized_document(data)
            if normalized.checksum_sha256 != row.content_checksum_sha256:
                raise ValueError("Normalized content checksum mismatch.")
            language = DeterministicMetadataExtractor().extract(document=normalized).language
        except (InvalidProvenanceError, MetadataExtractionError, ValueError) as exc:
            raise EvidenceIntegrityError("Graph language metadata is invalid.") from exc
        self._language_cache[cache_key] = language
        return language
