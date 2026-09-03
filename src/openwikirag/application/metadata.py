"""Deterministic, evidence-backed metadata for normalized documents."""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.extraction import NormalizedDocument, SourceSpan

METADATA_SCHEMA_VERSION = "document-metadata-v1"
type MetadataLanguage = Literal["en", "und"]


class MetadataExtractionError(Exception):
    """Raised when normalized content cannot produce valid metadata."""


class MetadataEvidence(BaseModel):
    """A metadata value mapped to a range in normalized document text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    normalized_start_char: int = Field(ge=0)
    normalized_end_char: int = Field(gt=0)
    section_path: tuple[str, ...] = ()
    page_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.normalized_end_char <= self.normalized_start_char:
            raise ValueError("Metadata evidence must have a positive range.")
        if any(not section for section in self.section_path):
            raise ValueError("Metadata evidence section paths cannot be empty.")
        return self


class MetadataHeading(MetadataEvidence):
    """A heading with its hierarchy depth and source evidence."""

    level: int = Field(ge=1, le=9)


class DocumentMetadata(BaseModel):
    """Versioned metadata that can later become a WikiRAG page input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["document-metadata-v1"] = "document-metadata-v1"
    metadata_version: str = Field(default="deterministic-metadata-v1", min_length=1)
    source_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    title_evidence: MetadataEvidence | None = None
    language: MetadataLanguage
    headings: tuple[MetadataHeading, ...] = ()
    dates: tuple[MetadataEvidence, ...] = ()
    authors: tuple[MetadataEvidence, ...] = ()

    def canonical_payload(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation."""

        return self.model_dump(mode="json", exclude_none=True)

    def canonical_bytes(self) -> bytes:
        """Serialize metadata deterministically for future artifact storage."""

        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def checksum_sha256(self) -> str:
        """Return the checksum of metadata, schema, and evidence together."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class DeterministicMetadataExtractor:
    """Extract conservative metadata without a model or external service."""

    metadata_version: str = "deterministic-metadata-v1"

    def extract(
        self,
        *,
        document: NormalizedDocument,
        title_hint: str | None = None,
        filename: str | None = None,
    ) -> DocumentMetadata:
        """Return metadata whose inferred values point into normalized text."""

        if not isinstance(document, NormalizedDocument) or not document.text.strip():
            raise MetadataExtractionError("Metadata requires normalized document text.")

        headings = tuple(_heading_metadata(document))
        title, title_evidence = _select_title(
            document,
            headings=headings,
            title_hint=title_hint,
            filename=filename,
        )
        return DocumentMetadata(
            metadata_version=self.metadata_version,
            source_type=document.source_type,
            title=title,
            title_evidence=title_evidence,
            language=_classify_language(document.text),
            headings=headings,
            dates=tuple(_date_metadata(document)),
            authors=tuple(_author_metadata(document)),
        )


_ENGLISH_MARKERS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)
_WORD_TOKEN = re.compile(r"\b[A-Za-z]{2,}\b")
_NON_EMPTY_LINE = re.compile(r"[^\r\n]+")
_ISO_DATE = re.compile(
    r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b"
)
_MONTH_DATE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|"
    r"Sep|Sept|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_AUTHOR_LABEL = re.compile(
    r"^[ \t]*authors?[ \t]*:[ \t]*(?P<values>[^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
_BYLINE = re.compile(
    r"^[ \t]*by[ \t]+(?P<values>[^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
_AUTHOR_SEPARATOR = re.compile(r",|;|\band\b", re.IGNORECASE)
_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _heading_metadata(document: NormalizedDocument) -> Iterable[MetadataHeading]:
    for span in document.spans:
        if span.kind != "heading" or not span.section_path:
            continue
        yield MetadataHeading(
            value=span.section_path[-1],
            raw_text=document.text[span.normalized_start_char : span.normalized_end_char],
            normalized_start_char=span.normalized_start_char,
            normalized_end_char=span.normalized_end_char,
            section_path=span.section_path,
            page_number=span.page_number,
            level=min(len(span.section_path), 9),
        )


def _select_title(
    document: NormalizedDocument,
    *,
    headings: tuple[MetadataHeading, ...],
    title_hint: str | None,
    filename: str | None,
) -> tuple[str, MetadataEvidence | None]:
    hinted_title = _collapse(title_hint or "")
    if hinted_title:
        return hinted_title, None

    if headings:
        heading = headings[0]
        return (
            heading.value,
            MetadataEvidence(
                value=heading.value,
                raw_text=heading.raw_text,
                normalized_start_char=heading.normalized_start_char,
                normalized_end_char=heading.normalized_end_char,
                section_path=heading.section_path,
                page_number=heading.page_number,
            ),
        )

    for match in _NON_EMPTY_LINE.finditer(document.text):
        start, end = _trimmed_range(document.text, match.start(), match.end())
        if start < end:
            value = _collapse(document.text[start:end])
            return value, _evidence(document, start=start, end=end, value=value)

    filename_title = _filename_title(filename)
    if filename_title:
        return filename_title, None
    raise MetadataExtractionError("No title candidate exists in normalized metadata input.")


def _classify_language(text: str) -> MetadataLanguage:
    tokens = {token.casefold() for token in _WORD_TOKEN.findall(text)}
    return "en" if len(tokens & _ENGLISH_MARKERS) >= 2 else "und"


def _date_metadata(document: NormalizedDocument) -> Iterable[MetadataEvidence]:
    candidates: list[tuple[int, int, str]] = []
    for match in _ISO_DATE.finditer(document.text):
        normalized = _valid_date(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
        )
        if normalized is not None:
            candidates.append((match.start(), match.end(), normalized))

    for match in _MONTH_DATE.finditer(document.text):
        normalized = _valid_date(
            year=int(match.group("year")),
            month=_MONTHS[match.group("month").casefold()],
            day=int(match.group("day")),
        )
        if normalized is not None:
            candidates.append((match.start(), match.end(), normalized))

    seen_values: set[str] = set()
    for start, end, value in sorted(candidates):
        if value in seen_values:
            continue
        seen_values.add(value)
        yield _evidence(document, start=start, end=end, value=value)


def _author_metadata(document: NormalizedDocument) -> Iterable[MetadataEvidence]:
    matches = sorted(
        (*_AUTHOR_LABEL.finditer(document.text), *_BYLINE.finditer(document.text)),
        key=lambda match: match.start(),
    )
    seen_ranges: set[tuple[int, int, str]] = set()
    for match in matches:
        values_start = match.start("values")
        values_end = match.end("values")
        cursor = values_start
        separators = _AUTHOR_SEPARATOR.finditer(document.text, values_start, values_end)
        for separator in (*separators, None):
            segment_end = values_end if separator is None else separator.start()
            start, end = _trimmed_range(document.text, cursor, segment_end)
            if start < end:
                value = _collapse(document.text[start:end])
                key = (start, end, value)
                if key not in seen_ranges:
                    seen_ranges.add(key)
                    yield _evidence(document, start=start, end=end, value=value)
            if separator is None:
                break
            cursor = separator.end()


def _valid_date(*, year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _evidence(
    document: NormalizedDocument,
    *,
    start: int,
    end: int,
    value: str,
) -> MetadataEvidence:
    context = _span_context(document.spans, start=start, end=end)
    return MetadataEvidence(
        value=value,
        raw_text=document.text[start:end],
        normalized_start_char=start,
        normalized_end_char=end,
        section_path=context.section_path if context is not None else (),
        page_number=context.page_number if context is not None else None,
    )


def _span_context(
    spans: tuple[SourceSpan, ...],
    *,
    start: int,
    end: int,
) -> SourceSpan | None:
    for span in spans:
        if span.normalized_start_char <= start < span.normalized_end_char:
            return span
    for span in spans:
        if span.normalized_start_char < end and start < span.normalized_end_char:
            return span
    return None


def _trimmed_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _filename_title(filename: str | None) -> str | None:
    if not filename or not filename.strip():
        return None
    basename = PurePosixPath(filename.replace("\\", "/")).name
    return _collapse(PurePosixPath(basename).stem)
