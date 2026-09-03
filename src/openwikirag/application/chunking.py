"""Deterministic hierarchical retrieval chunks with source provenance."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.extraction import NormalizedDocument, SourceSpan

CHUNK_SCHEMA_VERSION: Literal["chunk-v1"] = "chunk-v1"
CHUNKING_CONFIG_SCHEMA_VERSION: Literal["chunking-config-v1"] = "chunking-config-v1"
type ChunkKind = Literal["parent", "child"]
_TOKEN_PATTERN = re.compile(r"\S+")


class ChunkingError(Exception):
    """Base error for deterministic chunk construction."""


class ChunkingInputError(ChunkingError):
    """Raised when source content or chunk boundaries are invalid."""


class ChunkingConfigurationError(ChunkingError):
    """Raised when chunking budgets cannot produce a progressing window."""


class Chunk(BaseModel):
    """One immutable retrieval unit anchored to normalized source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["chunk-v1"] = CHUNK_SCHEMA_VERSION
    chunk_id: str = Field(pattern=r"^chunk-[0-9a-f]{64}$")
    chunk_kind: ChunkKind
    source_artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_chunk_id: str | None = Field(default=None, pattern=r"^chunk-[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    normalized_start_char: int = Field(ge=0)
    normalized_end_char: int = Field(gt=0)
    section_path: tuple[str, ...] = ()
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    token_count: int = Field(ge=1)
    character_count: int = Field(ge=1)
    content_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        """Keep offsets, counts, checksums, and hierarchy shape self-consistent."""

        if self.normalized_end_char <= self.normalized_start_char:
            raise ValueError("Chunk character range must be positive.")
        if self.character_count != len(self.text):
            raise ValueError("Chunk character count does not match its text.")
        if self.normalized_end_char - self.normalized_start_char != self.character_count:
            raise ValueError("Chunk range does not match its character count.")
        if self.token_count != _count_tokens(self.text):
            raise ValueError("Chunk token count does not match its text.")
        if _text_checksum(self.text) != self.content_checksum_sha256:
            raise ValueError("Chunk content checksum does not match its text.")
        if self.chunk_kind == "parent" and self.parent_chunk_id is not None:
            raise ValueError("Parent chunks cannot reference a parent chunk.")
        if self.chunk_kind == "child" and self.parent_chunk_id is None:
            raise ValueError("Child chunks require a parent chunk id.")
        if self.page_start is not None and self.page_end is not None:
            if self.page_end < self.page_start:
                raise ValueError("Chunk page range is invalid.")
        if any(not section for section in self.section_path):
            raise ValueError("Chunk section paths cannot contain empty labels.")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible chunk representation."""

        return self.model_dump(mode="json", exclude_none=True)

    def canonical_bytes(self) -> bytes:
        """Serialize the chunk deterministically for future persistence."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Explicit parent/child budgets for one deterministic chunking run."""

    parent_max_tokens: int = 400
    parent_max_characters: int = 2_000
    child_max_tokens: int = 120
    child_max_characters: int = 600
    child_overlap_tokens: int = 20

    def __post_init__(self) -> None:
        values = (
            self.parent_max_tokens,
            self.parent_max_characters,
            self.child_max_tokens,
            self.child_max_characters,
        )
        if any(value < 1 for value in values):
            raise ChunkingConfigurationError("Chunk budgets must be positive.")
        if self.child_overlap_tokens < 0:
            raise ChunkingConfigurationError("Child overlap cannot be negative.")
        if self.child_overlap_tokens >= self.child_max_tokens:
            raise ChunkingConfigurationError(
                "Child overlap must be smaller than the child token budget."
            )

    def canonical_payload(self) -> dict[str, int | str]:
        """Return the server-owned configuration identity used by artifacts."""

        return {
            "schema_version": CHUNKING_CONFIG_SCHEMA_VERSION,
            "parent_max_tokens": self.parent_max_tokens,
            "parent_max_characters": self.parent_max_characters,
            "child_max_tokens": self.child_max_tokens,
            "child_max_characters": self.child_max_characters,
            "child_overlap_tokens": self.child_overlap_tokens,
        }

    def canonical_bytes(self) -> bytes:
        """Serialize the validated configuration deterministically."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the stable checksum for this validated configuration."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkingResult:
    """Ordered parent and child chunks emitted for one normalized document."""

    chunks: tuple[Chunk, ...]

    @property
    def parents(self) -> tuple[Chunk, ...]:
        """Return structural parent chunks in deterministic order."""

        return tuple(chunk for chunk in self.chunks if chunk.chunk_kind == "parent")

    @property
    def children(self) -> tuple[Chunk, ...]:
        """Return bounded child chunks in deterministic order."""

        return tuple(chunk for chunk in self.chunks if chunk.chunk_kind == "child")


@dataclass(frozen=True, slots=True)
class _SectionRange:
    start_char: int
    end_char: int
    section_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Window:
    start_char: int
    end_char: int
    token_count: int


@dataclass(frozen=True, slots=True)
class HierarchicalChunker:
    """Create heading-aware parent context and bounded child windows."""

    config: ChunkingConfig = ChunkingConfig()

    def chunk(self, document: NormalizedDocument) -> ChunkingResult:
        """Return deterministic chunks or fail before returning a partial result."""

        if not isinstance(document, NormalizedDocument):
            raise ChunkingInputError("Chunking requires a normalized document.")
        token_spans = tuple(_TOKEN_PATTERN.finditer(document.text))
        if not token_spans:
            raise ChunkingInputError("Chunking requires at least one non-whitespace token.")

        chunks: list[Chunk] = []
        for section in _section_ranges(document):
            parent_windows = _window_ranges(
                text=document.text,
                token_spans=token_spans,
                section=section,
                max_tokens=self.config.parent_max_tokens,
                max_characters=self.config.parent_max_characters,
                overlap_tokens=0,
            )
            for parent_window in parent_windows:
                parent = _make_chunk(
                    document=document,
                    chunk_kind="parent",
                    parent_chunk_id=None,
                    section_path=section.section_path,
                    window=parent_window,
                )
                chunks.append(parent)
                child_windows = _window_ranges(
                    text=document.text,
                    token_spans=token_spans,
                    section=_SectionRange(
                        start_char=parent_window.start_char,
                        end_char=parent_window.end_char,
                        section_path=section.section_path,
                    ),
                    max_tokens=self.config.child_max_tokens,
                    max_characters=self.config.child_max_characters,
                    overlap_tokens=self.config.child_overlap_tokens,
                )
                chunks.extend(
                    _make_chunk(
                        document=document,
                        chunk_kind="child",
                        parent_chunk_id=parent.chunk_id,
                        section_path=section.section_path,
                        window=child_window,
                    )
                    for child_window in child_windows
                )

        if not chunks:
            raise ChunkingInputError("Chunking produced no retrieval units.")
        return ChunkingResult(chunks=tuple(chunks))


def _section_ranges(document: NormalizedDocument) -> tuple[_SectionRange, ...]:
    headings = tuple(
        span for span in document.spans if span.kind == "heading" and span.section_path
    )
    if not headings:
        return (_SectionRange(0, len(document.text), ()),)

    root_depth = min(len(span.section_path) for span in headings)
    roots = tuple(span for span in headings if len(span.section_path) == root_depth)
    sections: list[_SectionRange] = []
    first_root_start = roots[0].normalized_start_char
    if first_root_start > 0:
        sections.append(_SectionRange(0, first_root_start, ()))
    for index, heading in enumerate(roots):
        end_char = (
            roots[index + 1].normalized_start_char
            if index + 1 < len(roots)
            else len(document.text)
        )
        sections.append(
            _SectionRange(
                start_char=heading.normalized_start_char,
                end_char=end_char,
                section_path=heading.section_path,
            )
        )
    return tuple(sections)


def _window_ranges(
    *,
    text: str,
    token_spans: tuple[re.Match[str], ...],
    section: _SectionRange,
    max_tokens: int,
    max_characters: int,
    overlap_tokens: int,
) -> tuple[_Window, ...]:
    relevant_tokens = tuple(
        token
        for token in token_spans
        if section.start_char <= token.start() and token.end() <= section.end_char
    )
    if not relevant_tokens:
        return ()

    windows: list[_Window] = []
    cursor = 0
    while cursor < len(relevant_tokens):
        candidate_end = min(cursor + max_tokens, len(relevant_tokens))
        while candidate_end > cursor:
            start_char = relevant_tokens[cursor].start()
            end_char = relevant_tokens[candidate_end - 1].end()
            if end_char - start_char <= max_characters:
                break
            candidate_end -= 1
        if candidate_end == cursor:
            raise ChunkingInputError(
                "A token is longer than the configured character budget."
            )

        window = _Window(
            start_char=relevant_tokens[cursor].start(),
            end_char=relevant_tokens[candidate_end - 1].end(),
            token_count=candidate_end - cursor,
        )
        windows.append(window)
        if candidate_end == len(relevant_tokens):
            break

        actual_overlap = min(overlap_tokens, max(window.token_count - 1, 0))
        next_cursor = candidate_end - actual_overlap
        if next_cursor <= cursor:
            raise ChunkingConfigurationError("Chunk overlap did not advance the window.")
        cursor = next_cursor

    del text  # The source text is retained in the caller and only bounds are needed here.
    return tuple(windows)


def _make_chunk(
    *,
    document: NormalizedDocument,
    chunk_kind: ChunkKind,
    parent_chunk_id: str | None,
    section_path: tuple[str, ...],
    window: _Window,
) -> Chunk:
    text = document.text[window.start_char : window.end_char]
    content_checksum = _text_checksum(text)
    chunk_id = _chunk_id(
        source_artifact_checksum=document.checksum_sha256,
        chunk_kind=chunk_kind,
        parent_chunk_id=parent_chunk_id,
        section_path=section_path,
        start_char=window.start_char,
        end_char=window.end_char,
        content_checksum=content_checksum,
    )
    page_start, page_end = _page_range(document.spans, window.start_char, window.end_char)
    return Chunk(
        chunk_id=chunk_id,
        chunk_kind=chunk_kind,
        source_artifact_checksum=document.checksum_sha256,
        parent_chunk_id=parent_chunk_id,
        text=text,
        normalized_start_char=window.start_char,
        normalized_end_char=window.end_char,
        section_path=section_path,
        page_start=page_start,
        page_end=page_end,
        token_count=window.token_count,
        character_count=len(text),
        content_checksum_sha256=content_checksum,
    )


def _chunk_id(
    *,
    source_artifact_checksum: str,
    chunk_kind: ChunkKind,
    parent_chunk_id: str | None,
    section_path: tuple[str, ...],
    start_char: int,
    end_char: int,
    content_checksum: str,
) -> str:
    payload = {
        "chunk_kind": chunk_kind,
        "content_checksum": content_checksum,
        "end_char": end_char,
        "parent_chunk_id": parent_chunk_id,
        "section_path": list(section_path),
        "source_artifact_checksum": source_artifact_checksum,
        "start_char": start_char,
    }
    identity = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"chunk-{hashlib.sha256(identity).hexdigest()}"


def _page_range(
    spans: tuple[SourceSpan, ...],
    start_char: int,
    end_char: int,
) -> tuple[int | None, int | None]:
    page_numbers = tuple(
        span.page_number
        for span in spans
        if span.page_number is not None
        and span.normalized_start_char < end_char
        and start_char < span.normalized_end_char
    )
    if not page_numbers:
        return None, None
    return min(page_numbers), max(page_numbers)


def _count_tokens(text: str) -> int:
    return sum(1 for _ in _TOKEN_PATTERN.finditer(text))


def _text_checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
