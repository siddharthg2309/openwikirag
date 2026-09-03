"""Proof for deterministic normalized-text extraction and provenance."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    DuplicateExtractorRegistrationError,
    EncryptedPdfError,
    ExtractorRegistry,
    InvalidPdfTextQualityError,
    InvalidProvenanceError,
    InvalidTextEncodingError,
    MalformedPdfError,
    MarkdownExtractor,
    NormalizedDocument,
    NoTextExtractedError,
    PdfTextQualityAssessment,
    PdfTextQualityClassifier,
    PlainTextExtractor,
    SourceSpan,
    UnsupportedSourceTypeError,
)


def _digital_pdf(*pages: str) -> bytes:
    """Build a minimal text PDF without introducing another runtime dependency."""

    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        contents = DecodedStreamObject()
        contents.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = contents

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


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


def test_pdf_quality_classifier_labels_page_coverage_deterministically() -> None:
    classifier = PdfTextQualityClassifier()

    sufficient = classifier.classify(page_texts=("First\n", "Second\n"))
    assert sufficient.status == "sufficient"
    assert sufficient.page_count == 2
    assert sufficient.text_page_count == 2
    assert sufficient.character_count == 13
    assert sufficient.non_whitespace_character_count == 11
    assert sufficient.reason_codes == ()
    assert sufficient.needs_ocr is False

    partial = classifier.classify(page_texts=("First\n", "", "Third\n"))
    assert partial.status == "partial"
    assert partial.page_count == 3
    assert partial.text_page_count == 2
    assert partial.reason_codes == ("PAGES_WITHOUT_EXTRACTED_TEXT",)
    assert partial.needs_ocr is True

    empty = classifier.classify(page_texts=(" ", "\n"))
    assert empty.status == "empty"
    assert empty.text_page_count == 0
    assert empty.reason_codes == ("NO_EXTRACTED_TEXT",)
    assert empty.needs_ocr is True

    with pytest.raises(InvalidPdfTextQualityError):
        PdfTextQualityAssessment(
            status="sufficient",
            page_count=2,
            text_page_count=1,
            character_count=1,
            non_whitespace_character_count=1,
            reason_codes=(),
        )


def test_pdf_is_deterministic_and_preserves_page_level_provenance() -> None:
    data = _digital_pdf("Alpha page", "Beta page")

    first = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="pdf", data=data)
    second = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="pdf", data=data)

    assert first.text == "Alpha page\nBeta page"
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert first.quality is not None
    assert first.quality.status == "sufficient"
    assert first.quality.needs_ocr is False
    assert first.canonical_payload()["quality"] == {
        "status": "sufficient",
        "page_count": 2,
        "text_page_count": 2,
        "character_count": 19,
        "non_whitespace_character_count": 17,
        "reason_codes": [],
        "needs_ocr": False,
    }
    assert [
        (
            span.normalized_start_char,
            span.normalized_end_char,
            span.source_start_char,
            span.source_end_char,
            span.page_number,
        )
        for span in first.spans
    ] == [
        (0, 11, 0, 10, 1),
        (11, 20, 0, 9, 2),
    ]


def test_pdf_with_empty_pages_retains_text_and_marks_ocr_needed() -> None:
    document = DEFAULT_EXTRACTOR_REGISTRY.extract(
        source_type="pdf",
        data=_digital_pdf("First page", "", "Third page"),
    )

    assert document.text == "First page\nThird page"
    assert document.quality is not None
    assert document.quality.status == "partial"
    assert document.quality.page_count == 3
    assert document.quality.text_page_count == 2
    assert document.quality.needs_ocr is True
    assert [span.page_number for span in document.spans] == [1, 3]


def test_pdf_rejects_malformed_encrypted_and_textless_inputs() -> None:
    with pytest.raises(MalformedPdfError):
        DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="pdf", data=b"%PDF-1.7")

    encrypted = PdfWriter()
    encrypted.add_blank_page(width=612, height=792)
    encrypted.encrypt("secret")
    encrypted_bytes = BytesIO()
    encrypted.write(encrypted_bytes)
    with pytest.raises(EncryptedPdfError):
        DEFAULT_EXTRACTOR_REGISTRY.extract(
            source_type="pdf",
            data=encrypted_bytes.getvalue(),
        )

    with pytest.raises(NoTextExtractedError):
        DEFAULT_EXTRACTOR_REGISTRY.extract(
            source_type="pdf",
            data=_digital_pdf(" "),
        )


@pytest.mark.parametrize(
    ("source_type", "data", "error_type"),
    [
        ("image", b"not an image", UnsupportedSourceTypeError),
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
