"""Deterministic text extraction contracts and provenance for source documents."""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal, Protocol

from pypdf import PdfReader
from pypdf.errors import PdfReadError

NORMALIZED_DOCUMENT_SCHEMA_VERSION = "normalized-document-v1"
type PdfTextQualityStatus = Literal["sufficient", "partial", "empty"]


class ExtractionError(Exception):
    """Base error for extraction failures that should not produce an artifact."""


class UnsupportedSourceTypeError(ExtractionError):
    """Raised when no registered extractor can process a source type."""


class DuplicateExtractorRegistrationError(ExtractionError):
    """Raised when two extractors claim the same source type."""


class InvalidTextEncodingError(ExtractionError):
    """Raised when a text-like source is not valid UTF-8."""


class MalformedPdfError(ExtractionError):
    """Raised when a PDF cannot be parsed or one page cannot be extracted."""


class EncryptedPdfError(ExtractionError):
    """Raised when extraction would require a password-handling policy."""


class NoTextExtractedError(ExtractionError):
    """Raised when canonicalization leaves no meaningful text."""


class InvalidProvenanceError(ExtractionError):
    """Raised when spans cannot safely describe the normalized text."""


class InvalidPdfTextQualityError(ExtractionError):
    """Raised when a PDF quality assessment violates its count invariants."""


@dataclass(frozen=True, slots=True)
class PdfTextQualityAssessment:
    """Deterministic page-coverage signal for a parsed PDF."""

    status: PdfTextQualityStatus
    page_count: int
    text_page_count: int
    character_count: int
    non_whitespace_character_count: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise InvalidPdfTextQualityError("PDF page count cannot be negative.")
        if self.text_page_count < 0 or self.text_page_count > self.page_count:
            raise InvalidPdfTextQualityError("PDF text-page count is inconsistent.")
        if self.character_count < 0:
            raise InvalidPdfTextQualityError("PDF character count cannot be negative.")
        if (
            self.non_whitespace_character_count < 0
            or self.non_whitespace_character_count > self.character_count
        ):
            raise InvalidPdfTextQualityError("PDF non-whitespace count is inconsistent.")

        expected_status: PdfTextQualityStatus
        expected_reasons: tuple[str, ...]
        if self.text_page_count == 0:
            expected_status = "empty"
            expected_reasons = ("NO_EXTRACTED_TEXT",)
        elif self.text_page_count == self.page_count:
            expected_status = "sufficient"
            expected_reasons = ()
        else:
            expected_status = "partial"
            expected_reasons = ("PAGES_WITHOUT_EXTRACTED_TEXT",)
        if self.status != expected_status or self.reason_codes != expected_reasons:
            raise InvalidPdfTextQualityError("PDF quality status does not match its counts.")

    @property
    def needs_ocr(self) -> bool:
        """Return whether a later OCR stage may need to process missing text."""

        return self.status != "sufficient"

    def canonical_payload(self) -> dict[str, object]:
        """Return the stable JSON representation embedded in PDF artifacts."""

        return {
            "status": self.status,
            "page_count": self.page_count,
            "text_page_count": self.text_page_count,
            "character_count": self.character_count,
            "non_whitespace_character_count": self.non_whitespace_character_count,
            "reason_codes": list(self.reason_codes),
            "needs_ocr": self.needs_ocr,
        }


@dataclass(frozen=True, slots=True)
class PdfTextQualityClassifier:
    """Classify PDF extraction quality using page coverage only."""

    def classify(self, *, page_texts: Sequence[str]) -> PdfTextQualityAssessment:
        """Return a quality assessment derived from normalized page observations."""

        if any(not isinstance(page_text, str) for page_text in page_texts):
            raise InvalidPdfTextQualityError("PDF page observations must be text strings.")
        text_page_count = sum(bool(page_text.strip()) for page_text in page_texts)
        character_count = sum(len(page_text) for page_text in page_texts)
        non_whitespace_character_count = sum(
            sum(not character.isspace() for character in page_text) for page_text in page_texts
        )
        reason_codes: tuple[str, ...]
        if text_page_count == 0:
            status: PdfTextQualityStatus = "empty"
            reason_codes = ("NO_EXTRACTED_TEXT",)
        elif text_page_count == len(page_texts):
            status = "sufficient"
            reason_codes = ()
        else:
            status = "partial"
            reason_codes = ("PAGES_WITHOUT_EXTRACTED_TEXT",)
        return PdfTextQualityAssessment(
            status=status,
            page_count=len(page_texts),
            text_page_count=text_page_count,
            character_count=character_count,
            non_whitespace_character_count=non_whitespace_character_count,
            reason_codes=reason_codes,
        )


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
    quality: PdfTextQualityAssessment | None = None

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

        payload: dict[str, object] = {
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
        if self.quality is not None:
            payload["quality"] = self.quality.canonical_payload()
        return payload

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


@dataclass(frozen=True, slots=True)
class PdfExtractor:
    """Extract digital PDF text with deterministic page-level provenance."""

    parser_version: str = "pypdf-6-page-text-quality-v1"
    source_type: str = "pdf"
    parser_name: str = "pypdf"
    quality_classifier: PdfTextQualityClassifier = field(
        default_factory=PdfTextQualityClassifier
    )

    def extract(self, data: bytes) -> NormalizedDocument:
        try:
            reader = PdfReader(BytesIO(data), strict=True)
        except (OSError, PdfReadError, ValueError) as exc:
            raise MalformedPdfError("The PDF could not be parsed.") from exc
        if reader.is_encrypted:
            raise EncryptedPdfError("Encrypted PDFs are not supported.")

        pages: list[tuple[int, str, tuple[_LineSegment, ...]]] = []
        page_texts: list[str] = []
        try:
            for page_number, page in enumerate(reader.pages, start=1):
                extracted_text = page.extract_text() or ""
                normalized_text, segments = _canonicalize_line_endings(extracted_text)
                page_texts.append(normalized_text)
                if not extracted_text.strip():
                    continue
                pages.append((page_number, normalized_text, segments))
        except (OSError, PdfReadError, ValueError) as exc:
            raise MalformedPdfError("A PDF page could not be extracted.") from exc

        if not pages:
            raise NoTextExtractedError("The PDF contains no extractable digital text.")
        quality = self.quality_classifier.classify(page_texts=page_texts)

        text_parts: list[str] = []
        spans: list[SourceSpan] = []
        normalized_offset = 0
        for page_index, (page_number, page_text, segments) in enumerate(pages):
            page_boundary = "" if page_text.endswith("\n") or page_index == len(pages) - 1 else "\n"
            text_parts.append(page_text + page_boundary)
            for segment_index, segment in enumerate(segments):
                normalized_end = normalized_offset + segment.normalized_end_char
                if page_boundary and segment_index == len(segments) - 1:
                    normalized_end += len(page_boundary)
                spans.append(
                    SourceSpan(
                        normalized_start_char=normalized_offset + segment.normalized_start_char,
                        normalized_end_char=normalized_end,
                        source_start_char=segment.source_start_char,
                        source_end_char=segment.source_end_char,
                        page_number=page_number,
                        section_path=(),
                    )
                )
            normalized_offset += len(page_text) + len(page_boundary)

        return NormalizedDocument(
            source_type=self.source_type,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            text="".join(text_parts),
            spans=tuple(spans),
            quality=quality,
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


DEFAULT_EXTRACTOR_REGISTRY = ExtractorRegistry(
    (PlainTextExtractor(), MarkdownExtractor(), PdfExtractor())
)


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
