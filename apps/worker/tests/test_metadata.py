"""Proof for deterministic metadata and normalized-text evidence mapping."""

from typing import cast

import pytest
from pydantic import ValidationError

from openwikirag.application.extraction import MarkdownExtractor, NormalizedDocument, SourceSpan
from openwikirag.application.metadata import (
    DeterministicMetadataExtractor,
    DocumentMetadata,
    MetadataEvidence,
    MetadataExtractionError,
)


def test_metadata_is_deterministic_and_preserves_heading_date_author_evidence() -> None:
    document = MarkdownExtractor().extract(
        b"# Runbook\nAuthors: Ada Lovelace, Grace Hopper and Alan Turing\n"
        b"Published: 2025-02-03\nThis is a guide for the platform team.\n"
    )
    extractor = DeterministicMetadataExtractor()

    first = extractor.extract(document=document, filename="fallback.md")
    second = extractor.extract(document=document, filename="fallback.md")

    assert first.title == "Runbook"
    assert first.title_evidence is not None
    assert first.title_evidence.raw_text == "# Runbook\n"
    assert first.language == "en"
    assert len(first.headings) == 1
    assert first.headings[0].value == "Runbook"
    assert first.headings[0].level == 1
    assert first.headings[0].section_path == ("Runbook",)
    assert [date.value for date in first.dates] == ["2025-02-03"]
    assert [author.value for author in first.authors] == [
        "Ada Lovelace",
        "Grace Hopper",
        "Alan Turing",
    ]
    assert first.authors[0].section_path == ("Runbook",)
    assert first.dates[0].raw_text == "2025-02-03"
    assert all(
        document.text[evidence.normalized_start_char : evidence.normalized_end_char]
        == evidence.raw_text
        for evidence in (*first.headings, *first.dates, *first.authors)
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert first.canonical_payload()["schema_version"] == "document-metadata-v1"


def test_title_hint_has_precedence_without_false_normalized_evidence() -> None:
    document = MarkdownExtractor().extract(b"# Source title\nThe body is here.\n")

    metadata = DeterministicMetadataExtractor().extract(
        document=document,
        title_hint="  User supplied\nname  ",
    )

    assert metadata.title == "User supplied name"
    assert metadata.title_evidence is None
    assert metadata.headings[0].value == "Source title"


def test_metadata_omits_invalid_or_ambiguous_dates_and_unknown_language() -> None:
    document = MarkdownExtractor().extract(
        b"Bonjour monde\nRelease: 2025-02-30 01/02/2025\n"
        b"Published January 4th, 2025 2025-01-04.\n"
    )

    metadata = DeterministicMetadataExtractor().extract(document=document)

    assert metadata.language == "und"
    assert [date.value for date in metadata.dates] == ["2025-01-04"]
    assert metadata.authors == ()


def test_metadata_uses_page_and_section_context_from_normalized_spans() -> None:
    text = "Policy\nAuthor: Ada Lovelace\n"
    document = NormalizedDocument(
        source_type="pdf",
        parser_name="test",
        parser_version="v1",
        text=text,
        spans=(
            SourceSpan(
                normalized_start_char=0,
                normalized_end_char=7,
                source_start_char=0,
                source_end_char=7,
                page_number=2,
                section_path=("Policy",),
                kind="heading",
            ),
            SourceSpan(
                normalized_start_char=7,
                normalized_end_char=len(text),
                source_start_char=7,
                source_end_char=len(text),
                page_number=2,
                section_path=("Policy",),
            ),
        ),
    )

    metadata = DeterministicMetadataExtractor().extract(document=document)

    assert metadata.headings[0].page_number == 2
    assert metadata.authors[0].page_number == 2
    assert metadata.authors[0].section_path == ("Policy",)


def test_metadata_models_reject_invalid_evidence_ranges_and_invalid_input() -> None:
    with pytest.raises(ValidationError, match="positive range"):
        MetadataEvidence(
            value="value",
            raw_text="value",
            normalized_start_char=4,
            normalized_end_char=4,
        )

    with pytest.raises(MetadataExtractionError, match="normalized document text"):
        DeterministicMetadataExtractor().extract(
            document=cast(NormalizedDocument, object())
        )


def test_metadata_model_round_trip_is_frozen_and_typed() -> None:
    document = MarkdownExtractor().extract(b"A short guide for the team.")
    metadata = DeterministicMetadataExtractor().extract(document=document)

    restored = DocumentMetadata.model_validate(metadata.model_dump())

    assert restored == metadata
    with pytest.raises(ValidationError):
        metadata.title = "changed"
