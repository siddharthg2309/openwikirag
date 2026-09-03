"""Typed, provenance-bearing WikiRAG page contracts."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.extraction import NormalizedDocument
from openwikirag.application.metadata import (
    DocumentMetadata,
    MetadataEvidence,
    MetadataHeading,
    MetadataLanguage,
)

WIKI_PAGE_SCHEMA_VERSION = "wiki-page-v1"


class WikiPageBuildError(Exception):
    """Raised when normalized content and metadata cannot form one page."""


class WikiDefinition(BaseModel):
    """A definition that must cite one or more normalized evidence ranges."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    evidence: tuple[MetadataEvidence, ...] = Field(min_length=1)


class WikiReference(BaseModel):
    """A named reference that carries evidence for a future generated page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    target: str = Field(min_length=1)
    evidence: tuple[MetadataEvidence, ...] = Field(min_length=1)


class WikiSection(BaseModel):
    """A structural page section anchored by its source heading."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str = Field(pattern=r"^section-[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    level: int = Field(ge=1, le=9)
    section_path: tuple[str, ...] = Field(min_length=1)
    heading: MetadataHeading

    @model_validator(mode="after")
    def validate_heading_consistency(self) -> Self:
        if self.title != self.heading.value:
            raise ValueError("Wiki section title must match heading evidence.")
        if self.level != self.heading.level:
            raise ValueError("Wiki section level must match heading evidence.")
        if self.section_path != self.heading.section_path:
            raise ValueError("Wiki section path must match heading evidence.")
        return self


class WikiPage(BaseModel):
    """An immutable-in-memory WikiRAG page skeleton derived from one version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["wiki-page-v1"] = "wiki-page-v1"
    page_version: str = Field(default="deterministic-skeleton-v1", min_length=1)
    generator_identity: str = Field(default="deterministic-skeleton-v1", min_length=1)
    source_type: str = Field(min_length=1)
    language: MetadataLanguage
    title: str = Field(min_length=1)
    title_evidence: MetadataEvidence | None = None
    source_artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_status: Literal["draft", "needs_review", "approved"] = "draft"
    sections: tuple[WikiSection, ...] = ()
    definitions: tuple[WikiDefinition, ...] = ()
    references: tuple[WikiReference, ...] = ()

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible page representation."""

        return self.model_dump(mode="json", exclude_none=True)

    def canonical_bytes(self) -> bytes:
        """Serialize the derived page deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the immutable identity of this page skeleton."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class WikiPageBuilder:
    """Build a deterministic page skeleton without calling an LLM."""

    page_version: str = "deterministic-skeleton-v1"

    def build(
        self,
        *,
        document: NormalizedDocument,
        metadata: DocumentMetadata,
    ) -> WikiPage:
        """Validate matching inputs and project metadata headings into sections."""

        if not isinstance(document, NormalizedDocument) or not isinstance(
            metadata, DocumentMetadata
        ):
            raise WikiPageBuildError("A page requires normalized content and metadata.")
        if metadata.source_type != document.source_type:
            raise WikiPageBuildError("Page metadata and normalized source types differ.")
        if metadata.source_artifact_checksum != document.checksum_sha256:
            raise WikiPageBuildError("Page metadata belongs to a different source artifact.")
        _validate_metadata_evidence(document, metadata)

        sections = tuple(
            WikiSection(
                section_id=_section_id(document, heading),
                title=heading.value,
                level=heading.level,
                section_path=heading.section_path,
                heading=heading,
            )
            for heading in metadata.headings
        )
        return WikiPage(
            page_version=self.page_version,
            source_type=document.source_type,
            language=metadata.language,
            title=metadata.title,
            title_evidence=metadata.title_evidence,
            source_artifact_checksum=document.checksum_sha256,
            metadata_checksum=metadata.checksum_sha256,
            sections=sections,
        )


def _section_id(document: NormalizedDocument, heading: MetadataHeading) -> str:
    seed = json.dumps(
        {
            "source_artifact_checksum": document.checksum_sha256,
            "title": heading.value,
            "level": heading.level,
            "section_path": list(heading.section_path),
            "normalized_start_char": heading.normalized_start_char,
            "normalized_end_char": heading.normalized_end_char,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"section-{hashlib.sha256(seed).hexdigest()}"


def _validate_metadata_evidence(
    document: NormalizedDocument,
    metadata: DocumentMetadata,
) -> None:
    for evidence in _metadata_evidence(metadata):
        if evidence.normalized_end_char > len(document.text):
            raise WikiPageBuildError("Metadata evidence exceeds normalized text.")
        if document.text[evidence.normalized_start_char : evidence.normalized_end_char] != (
            evidence.raw_text
        ):
            raise WikiPageBuildError("Metadata evidence does not match normalized text.")


def _metadata_evidence(metadata: DocumentMetadata) -> Iterable[MetadataEvidence]:
    if metadata.title_evidence is not None:
        yield metadata.title_evidence
    yield from metadata.headings
    yield from metadata.dates
    yield from metadata.authors
