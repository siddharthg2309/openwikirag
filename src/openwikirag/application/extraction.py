"""Deterministic text extraction contracts and provenance for source documents."""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

NORMALIZED_DOCUMENT_SCHEMA_VERSION = "normalized-document-v1"


class ExtractionError(Exception):
    """Base error for extraction failures that should not produce an artifact."""


class UnsupportedSourceTypeError(ExtractionError):
    """Raised when no registered extractor can process a source type."""


class DuplicateExtractorRegistrationError(ExtractionError):
    """Raised when two extractors claim the same source type."""


class InvalidTextEncodingError(ExtractionError):
    """Raised when a text-like source is not valid UTF-8."""


class NoTextExtractedError(ExtractionError):
    """Raised when canonicalization leaves no meaningful text."""


class InvalidProvenanceError(ExtractionError):
    """Raised when spans cannot safely describe the normalized text."""


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A contiguous normalized range mapped to decoded-source context."""

    normalized_start_char: int
    normalized_end_char: int
    source_start_char: int
    source_end_char: int
    page_number: int | None
    section_path: tuple[str, ...]
    kind: str = "text"

    def __post_init__(self) -> None:
        if self.normalized_start_char < 0 or self.source_start_char < 0:
            raise InvalidProvenanceError("Character offsets cannot be negative.")
        if self.normalized_end_char <= self.normalized_start_char:
            raise InvalidProvenanceError("A normalized span must contain text.")
        if self.source_end_char <= self.source_start_char:
            raise InvalidProvenanceError("A source span must contain text.")
        if self.page_number is not None and self.page_number < 1:
            raise InvalidProvenanceError("Page numbers start at one.")
        if not self.kind:
            raise InvalidProvenanceError("A provenance span requires a kind.")
        if any(not section for section in self.section_path):
            raise InvalidProvenanceError("Section paths cannot contain empty labels.")


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """Canonical text and provenance emitted by every future extractor."""

    source_type: str
    parser_name: str
    parser_version: str
    text: str
    spans: tuple[SourceSpan, ...]

    def __post_init__(self) -> None:
        if not self.source_type or not self.parser_name or not self.parser_version:
            raise InvalidProvenanceError("Source type and parser identity are required.")
        if not self.text.strip():
            raise NoTextExtractedError("The document contains no meaningful normalized text.")
        if not self.spans:
            raise InvalidProvenanceError("Normalized text requires provenance spans.")

        expected_start = 0
        for span in self.spans:
            if span.normalized_start_char != expected_start:
                raise InvalidProvenanceError(
                    "Normalized provenance spans must be contiguous and ordered."
                )
            if span.normalized_end_char > len(self.text):
                raise InvalidProvenanceError("A span exceeds the normalized text length.")
            expected_start = span.normalized_end_char
        if expected_start != len(self.text):
            raise InvalidProvenanceError("Provenance spans must cover all normalized text.")

    def canonical_payload(self) -> dict[str, object]:
        """Return an intentionally stable artifact representation for persistence."""

        return {
            "schema_version": NORMALIZED_DOCUMENT_SCHEMA_VERSION,
            "source_type": self.source_type,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "text": self.text,
            "spans": [
                {
                    "normalized_start_char": span.normalized_start_char,
                    "normalized_end_char": span.normalized_end_char,
                    "source_start_char": span.source_start_char,
                    "source_end_char": span.source_end_char,
                    "page_number": span.page_number,
                    "section_path": list(span.section_path),
                    "kind": span.kind,
                }
                for span in self.spans
            ],
        }

    def canonical_bytes(self) -> bytes:
        """Serialize deterministically so parser artifacts are reproducible."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Checksum the text, provenance, and parser identity together."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class DocumentExtractor(Protocol):
    """Pure parser contract that keeps extraction separate from worker I/O."""

    @property
    def source_type(self) -> str:
        """Return the validated source type handled by this extractor."""

    def extract(self, data: bytes) -> NormalizedDocument:
        """Produce one canonical normalized document or raise an extraction error."""


@dataclass(frozen=True, slots=True)
class PlainTextExtractor:
    """Extract UTF-8 text while retaining decoded-source character offsets."""

    parser_version: str = "utf8-text-v1"
    source_type: str = "text"
    parser_name: str = "utf8-text"

    def extract(self, data: bytes) -> NormalizedDocument:
        text, segments = _decode_and_canonicalize(data)
        spans = tuple(_span_for_segment(segment) for segment in segments)
        return NormalizedDocument(
            source_type=self.source_type,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            text=text,
            spans=spans,
        )


@dataclass(frozen=True, slots=True)
class MarkdownExtractor:
    """Extract UTF-8 Markdown and retain heading-derived section hierarchy."""

    parser_version: str = "markdown-v1"
    source_type: str = "markdown"
    parser_name: str = "markdown"

    def extract(self, data: bytes) -> NormalizedDocument:
        text, segments = _decode_and_canonicalize(data)
        section_path: list[str] = []
        spans: list[SourceSpan] = []
        for segment in segments:
            line = text[segment.normalized_start_char : segment.normalized_end_char]
            heading = _markdown_heading(line.removesuffix("\n"))
            kind = "text"
            if heading is not None:
                level, title = heading
                section_path = section_path[: level - 1]
                section_path.append(title)
                kind = "heading"
            spans.append(
                SourceSpan(
                    normalized_start_char=segment.normalized_start_char,
                    normalized_end_char=segment.normalized_end_char,
                    source_start_char=segment.source_start_char,
                    source_end_char=segment.source_end_char,
                    page_number=None,
                    section_path=tuple(section_path),
                    kind=kind,
                )
            )
        return NormalizedDocument(
            source_type=self.source_type,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            text=text,
            spans=tuple(spans),
        )


class ExtractorRegistry:
    """Select exactly one pure extractor for a validated document source type."""

    def __init__(self, extractors: Iterable[DocumentExtractor]) -> None:
        self._extractors: dict[str, DocumentExtractor] = {}
        for extractor in extractors:
            if extractor.source_type in self._extractors:
                raise DuplicateExtractorRegistrationError(
                    f"An extractor is already registered for '{extractor.source_type}'."
                )
            self._extractors[extractor.source_type] = extractor

    def extract(self, *, source_type: str, data: bytes) -> NormalizedDocument:
        extractor = self._extractors.get(source_type)
        if extractor is None:
            raise UnsupportedSourceTypeError(
                f"No extractor is registered for '{source_type}'."
            )
        return extractor.extract(data)


DEFAULT_EXTRACTOR_REGISTRY = ExtractorRegistry((PlainTextExtractor(), MarkdownExtractor()))


@dataclass(frozen=True, slots=True)
class _LineSegment:
    normalized_start_char: int
    normalized_end_char: int
    source_start_char: int
    source_end_char: int


_LINE_ENDING = re.compile(r"\r\n|\r|\n")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _decode_and_canonicalize(data: bytes) -> tuple[str, tuple[_LineSegment, ...]]:
    try:
        source_text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidTextEncodingError("Text-like sources must be valid UTF-8.") from exc

    normalized_text, segments = _canonicalize_line_endings(source_text)
    if not normalized_text.strip():
        raise NoTextExtractedError("The document contains no meaningful normalized text.")
    return normalized_text, segments


def _canonicalize_line_endings(source_text: str) -> tuple[str, tuple[_LineSegment, ...]]:
    """Convert only CRLF/CR endings while retaining source-range provenance."""

    normalized_parts: list[str] = []
    segments: list[_LineSegment] = []
    source_start = 0
    normalized_start = 0
    for match in _LINE_ENDING.finditer(source_text):
        source_end = match.end()
        normalized_line = source_text[source_start : match.start()] + "\n"
        normalized_end = normalized_start + len(normalized_line)
        normalized_parts.append(normalized_line)
        segments.append(
            _LineSegment(
                normalized_start_char=normalized_start,
                normalized_end_char=normalized_end,
                source_start_char=source_start,
                source_end_char=source_end,
            )
        )
        source_start = source_end
        normalized_start = normalized_end

    if source_start < len(source_text):
        normalized_line = source_text[source_start:]
        normalized_end = normalized_start + len(normalized_line)
        normalized_parts.append(normalized_line)
        segments.append(
            _LineSegment(
                normalized_start_char=normalized_start,
                normalized_end_char=normalized_end,
                source_start_char=source_start,
                source_end_char=len(source_text),
            )
        )

    return "".join(normalized_parts), tuple(segments)


def _span_for_segment(segment: _LineSegment) -> SourceSpan:
    return SourceSpan(
        normalized_start_char=segment.normalized_start_char,
        normalized_end_char=segment.normalized_end_char,
        source_start_char=segment.source_start_char,
        source_end_char=segment.source_end_char,
        page_number=None,
        section_path=(),
    )


def _markdown_heading(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING.fullmatch(line)
    if match is None:
        return None
    title = match.group(2).strip().rstrip("#").rstrip()
    if not title:
        return None
    return len(match.group(1)), title
