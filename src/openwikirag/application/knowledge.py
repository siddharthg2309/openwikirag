"""Canonical, explicitly asserted graph facts with exact source evidence."""

import hashlib
import re
import unicodedata
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from openwikirag.application.evidence import read_manifest
from openwikirag.infrastructure.repositories.knowledge import KnowledgeRepository
from openwikirag.infrastructure.storage import ObjectStorage

Predicate = Literal["uses", "depends_on", "calls", "owned_by"]
EXTRACTOR: Literal["explicit-relations-v1"] = "explicit-relations-v1"
_RELATION = re.compile(
    r"^\[([^\]\n]{1,120})\][ \t]+--(uses|depends_on|calls|owned_by)-->"
    r"[ \t]+\[([^\]\n]{1,120})\][ \t]*\.?$",
    re.MULTILINE,
)


class KnowledgeError(Exception):
    """Canonical graph evidence could not be safely constructed or loaded."""


def normalize_entity(name: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    if not value or len(value) > 120 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Invalid entity name.")
    return value


def entity_id(tenant_id: UUID, name: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{normalize_entity(name)}".encode()).hexdigest()


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=120)


class RelationFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    predicate: Predicate
    chunk_id: str = Field(pattern=r"^chunk-[0-9a-f]{64}$")
    quote: str = Field(min_length=1, max_length=300)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)


class KnowledgeArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["knowledge-v1"] = "knowledge-v1"
    extractor: Literal["explicit-relations-v1"] = EXTRACTOR
    id: UUID
    tenant_id: UUID
    document_id: UUID
    document_version_id: UUID
    manifest_id: UUID
    manifest_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: tuple[Entity, ...] = Field(max_length=1000)
    facts: tuple[RelationFact, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.id != uuid5(self.manifest_id, self.extractor):
            raise ValueError("Invalid artifact identity.")
        entities = {item.id: item for item in self.entities}
        if len(entities) != len(self.entities) or len({fact.id for fact in self.facts}) != len(
            self.facts
        ):
            raise ValueError("Duplicate entity or fact identity.")
        if any(
            item.name != normalize_entity(item.name)
            or item.id != entity_id(self.tenant_id, item.name)
            for item in self.entities
        ):
            raise ValueError("Entity normalization or identity mismatch.")
        for fact in self.facts:
            if fact.subject_id not in entities or fact.object_id not in entities:
                raise ValueError("Relation endpoint is missing.")
            if fact.end_char - fact.start_char != len(fact.quote):
                raise ValueError("Evidence range mismatch.")
            expected = hashlib.sha256(
                f"{self.id}:{fact.chunk_id}:{fact.start_char}:{fact.quote}".encode()
            ).hexdigest()
            if fact.id != expected:
                raise ValueError("Fact identity mismatch.")
            match = _RELATION.fullmatch(fact.quote)
            if (
                match is None
                or match.group(2) != fact.predicate
                or entity_id(self.tenant_id, match.group(1)) != fact.subject_id
                or entity_id(self.tenant_id, match.group(3)) != fact.object_id
            ):
                raise ValueError("Relationship is not supported by its explicit quotation.")
        return self

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class KnowledgeArtifactService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self.repository = KnowledgeRepository(session)
        self.storage = storage

    async def build(self, *, tenant_id: UUID, manifest_id: UUID) -> KnowledgeArtifact:
        try:
            source = await self.repository.source(tenant_id=tenant_id, manifest_id=manifest_id)
            if source is None:
                raise KnowledgeError("Graph source is unavailable.")
            row, version = source
            data = await self.storage.get(object_key=row.artifact_object_key)
            if hashlib.sha256(data).hexdigest() != row.manifest_checksum_sha256:
                raise KnowledgeError("Graph source checksum mismatch.")
            manifest = read_manifest(data)
            if (
                manifest.source_artifact_checksum != row.source_artifact_checksum
                or manifest.chunking_config_checksum != row.chunking_config_checksum
            ):
                raise KnowledgeError("Graph source lineage mismatch.")
            artifact_id = uuid5(manifest_id, EXTRACTOR)
            entities: dict[str, Entity] = {}
            facts = []
            # Parent passages avoid duplicate assertions from child overlap.
            for chunk in manifest.chunks:
                if chunk.chunk_kind != "parent":
                    continue
                for match in _RELATION.finditer(chunk.text):
                    names = (normalize_entity(match.group(1)), normalize_entity(match.group(3)))
                    ids = tuple(entity_id(tenant_id, name) for name in names)
                    for name, identity in zip(names, ids, strict=True):
                        entities[identity] = Entity(id=identity, name=name)
                    start = chunk.normalized_start_char + match.start()
                    quote = match.group(0)
                    facts.append(
                        RelationFact(
                            id=hashlib.sha256(
                                f"{artifact_id}:{chunk.chunk_id}:{start}:{quote}".encode()
                            ).hexdigest(),
                            subject_id=ids[0],
                            object_id=ids[1],
                            predicate=cast(Predicate, match.group(2)),
                            chunk_id=chunk.chunk_id,
                            quote=quote,
                            start_char=start,
                            end_char=start + len(quote),
                        )
                    )
                    if len(facts) > 1000 or len(entities) > 1000:
                        raise KnowledgeError("Graph artifact exceeds its bound.")
            artifact = KnowledgeArtifact(
                id=artifact_id,
                tenant_id=tenant_id,
                document_id=version.document_id,
                document_version_id=version.id,
                manifest_id=manifest_id,
                manifest_checksum=row.manifest_checksum_sha256,
                entities=tuple(entities[key] for key in sorted(entities)),
                facts=tuple(sorted(facts, key=lambda item: item.id)),
            )
            await self.repository.put(artifact.model_dump(mode="json"), artifact.checksum)
            return artifact
        except KnowledgeError:
            raise
        except Exception as exc:
            raise KnowledgeError("Canonical graph construction failed.") from exc
