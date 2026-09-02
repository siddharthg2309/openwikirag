"""Proof for deterministic normalized-text extraction and provenance."""

import pytest

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    DuplicateExtractorRegistrationError,
    ExtractorRegistry,
    InvalidProvenanceError,
    InvalidTextEncodingError,
    MarkdownExtractor,
    NormalizedDocument,
    NoTextExtractedError,
    PlainTextExtractor,
    SourceSpan,
    UnsupportedSourceTypeError,
)


def test_plain_text_is_deterministic_and_preserves_line_offset_mapping() -> None:
    data = b"Alpha\r\nBeta\rGamma\nDelta"

    first = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="text", data=data)
    second = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="text", data=data)

    assert first.text == "Alpha\nBeta\nGamma\nDelta"
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert [
        (
            span.normalized_start_char,
            span.normalized_end_char,
            span.source_start_char,
            span.source_end_char,
        )
        for span in first.spans
    ] == [
        (0, 6, 0, 7),
        (6, 11, 7, 12),
        (11, 17, 12, 18),
        (17, 22, 18, 23),
    ]
    assert first.spans[-1].normalized_end_char == len(first.text)


def test_markdown_heading_paths_cover_every_normalized_character() -> None:
    document = DEFAULT_EXTRACTOR_REGISTRY.extract(
        source_type="markdown",
        data=b"# Overview\r\nBody\n## Scope\rMore\n# Next\nDone",
    )

    assert document.text == "# Overview\nBody\n## Scope\nMore\n# Next\nDone"
    assert [span.section_path for span in document.spans] == [
        ("Overview",),
        ("Overview",),
        ("Overview", "Scope"),
        ("Overview", "Scope"),
        ("Next",),
        ("Next",),
    ]
    assert [span.kind for span in document.spans] == [
        "heading",
        "text",
        "heading",
        "text",
        "heading",
        "text",
    ]
    assert document.spans[2].source_start_char == 17
    assert document.spans[2].source_end_char == 26
    assert document.spans[-1].normalized_end_char == len(document.text)


def test_parser_version_is_part_of_the_canonical_artifact_identity() -> None:
    data = b"Same text\n"

    first = PlainTextExtractor(parser_version="utf8-text-v1").extract(data)
    second = PlainTextExtractor(parser_version="utf8-text-v2").extract(data)

    assert first.text == second.text
    assert first.checksum_sha256 != second.checksum_sha256


@pytest.mark.parametrize(
    ("source_type", "data", "error_type"),
    [
        ("pdf", b"%PDF-1.7", UnsupportedSourceTypeError),
        ("text", b"\xff", InvalidTextEncodingError),
        ("markdown", b" \r\n\t", NoTextExtractedError),
    ],
)
def test_extraction_rejects_unsupported_invalid_and_blank_inputs(
    source_type: str,
    data: bytes,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        DEFAULT_EXTRACTOR_REGISTRY.extract(source_type=source_type, data=data)


def test_registry_and_artifact_validate_duplicate_and_gapped_provenance() -> None:
    with pytest.raises(DuplicateExtractorRegistrationError):
        ExtractorRegistry((PlainTextExtractor(), PlainTextExtractor()))

    with pytest.raises(InvalidProvenanceError):
        NormalizedDocument(
            source_type="text",
            parser_name="test",
            parser_version="v1",
            text="abc",
            spans=(
                SourceSpan(
                    normalized_start_char=1,
                    normalized_end_char=3,
                    source_start_char=0,
                    source_end_char=2,
                    page_number=None,
                    section_path=(),
                ),
            ),
        )


def test_markdown_extractor_has_its_own_source_type() -> None:
    assert MarkdownExtractor().source_type == "markdown"
