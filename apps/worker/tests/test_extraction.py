"""Proof for deterministic normalized-text extraction and provenance."""

from collections.abc import Sequence
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from openwikirag.application.extraction import (
    DEFAULT_EXTRACTOR_REGISTRY,
    DocxArchiveTooLargeError,
    DocxExtractor,
    DuplicateExtractorRegistrationError,
    EncryptedPdfError,
    ExtractorRegistry,
    InvalidPdfTextQualityError,
    InvalidProvenanceError,
    InvalidTextEncodingError,
    MalformedDocxError,
    MalformedPdfError,
    MarkdownExtractor,
    NormalizedDocument,
    NoTextExtractedError,
    OcrMetadata,
    PdfExtractor,
    PdfTextQualityAssessment,
    PdfTextQualityClassifier,
    PlainTextExtractor,
    SourceSpan,
    UnsupportedSourceTypeError,
)
from openwikirag.application.ocr import OcrPageResult

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


def _docx_paragraph(text: str, *, style: str | None = None, content: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    paragraph_content = (
        content
        if content is not None
        else f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
    )
    return f"<w:p>{properties}{paragraph_content}</w:p>"


def _docx_archive(document_body: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    document_xml = (
        f'<w:document xmlns:w="{_WORD_NAMESPACE}">'
        f"<w:body>{document_body}<w:sectPr/></w:body></w:document>"
    ).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return output.getvalue()


class FakeOcrFallback:
    artifact_identity = "ocr-test-v1"

    def __init__(self, page_text: dict[int, str]) -> None:
        self.page_text = page_text
        self.calls: list[tuple[int, ...]] = []

    def extract(
        self,
        *,
        pdf_data: bytes,
        page_numbers: Sequence[int],
    ) -> tuple[OcrPageResult, ...]:
        del pdf_data
        requested_pages = tuple(page_numbers)
        self.calls.append(requested_pages)
        return tuple(
            OcrPageResult(page_number=page_number, text=self.page_text[page_number])
            for page_number in requested_pages
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


def test_docx_is_deterministic_and_preserves_heading_and_paragraph_provenance() -> None:
    data = _docx_archive(
        "".join(
            (
                _docx_paragraph("Overview", style="Heading1"),
                _docx_paragraph(
                    "",
                    content=(
                        "<w:r><w:t>Body</w:t><w:tab/><w:t>value</w:t>"
                        "<w:br/><w:t>continued</w:t></w:r>"
                    ),
                ),
                _docx_paragraph("Scope", style="Heading2"),
                _docx_paragraph("Details"),
                "<w:tbl><w:tr><w:tc>"
                + _docx_paragraph("Table detail")
                + "</w:tc></w:tr></w:tbl>",
            )
        )
    )

    first = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="docx", data=data)
    second = DEFAULT_EXTRACTOR_REGISTRY.extract(source_type="docx", data=data)

    assert first.text == "Overview\nBody\tvalue\ncontinued\nScope\nDetails\nTable detail"
    assert first.parser_name == "docx-xml"
    assert first.parser_version == "ooxml-xml-v1"
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert [span.kind for span in first.spans] == [
        "heading",
        "text",
        "heading",
        "text",
        "text",
    ]
    assert [span.section_path for span in first.spans] == [
        ("Overview",),
        ("Overview",),
        ("Overview", "Scope"),
        ("Overview", "Scope"),
        ("Overview", "Scope"),
    ]
    assert [
        (span.normalized_start_char, span.normalized_end_char)
        for span in first.spans
    ] == [(0, 8), (8, 29), (29, 35), (35, 43), (43, 56)]
    assert first.spans[-1].source_start_char == first.spans[-1].normalized_start_char
    assert first.spans[-1].normalized_end_char == len(first.text)


def test_docx_excludes_deleted_text_and_blank_paragraphs() -> None:
    data = _docx_archive(
        "".join(
            (
                _docx_paragraph(
                    "",
                    content=(
                        "<w:del><w:r><w:delText>deleted</w:delText></w:r></w:del>"
                        "<w:r><w:t>kept</w:t></w:r>"
                    ),
                ),
                _docx_paragraph(" \t", content="<w:r><w:tab/></w:r>"),
            )
        )
    )

    document = DocxExtractor().extract(data)

    assert document.text == "kept"
    assert len(document.spans) == 1
    assert document.spans[0].kind == "text"


def test_docx_rejects_malformed_missing_blank_and_oversized_inputs() -> None:
    with pytest.raises(MalformedDocxError):
        DocxExtractor().extract(b"not a zip")

    missing_main_part = BytesIO()
    with ZipFile(missing_main_part, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
    with pytest.raises(MalformedDocxError, match="missing a required"):
        DocxExtractor().extract(missing_main_part.getvalue())

    blank_docx = _docx_archive(_docx_paragraph(" \t"))
    with pytest.raises(NoTextExtractedError):
        DocxExtractor().extract(blank_docx)

    oversized_docx = _docx_archive("", extra={"word/media/image.bin": b"x" * 128})
    with pytest.raises(DocxArchiveTooLargeError):
        DocxExtractor(max_uncompressed_bytes=64).extract(oversized_docx)


def test_docx_rejects_invalid_main_document_xml() -> None:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<not-xml")

    with pytest.raises(MalformedDocxError, match="XML is invalid"):
        DocxExtractor().extract(output.getvalue())


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


def test_pdf_ocr_fallback_merges_only_missing_pages_with_explicit_provenance() -> None:
    fallback = FakeOcrFallback({2: "Scanned page"})
    extractor = PdfExtractor(ocr_fallback=fallback)

    document = extractor.extract(data=_digital_pdf("First page", "", "Third page"))

    assert fallback.calls == [(2,)]
    assert document.text == "First page\nScanned page\nThird page"
    assert document.parser_name == "pypdf-ocr"
    assert document.parser_version == "pypdf-6-page-text-quality-v1+ocr-test-v1"
    assert document.quality is not None
    assert document.quality.status == "sufficient"
    assert document.quality.needs_ocr is False
    assert document.ocr == OcrMetadata(
        pipeline_identity="ocr-test-v1",
        attempted_page_numbers=(2,),
        recovered_page_numbers=(2,),
    )
    assert [span.page_number for span in document.spans] == [1, 2, 3]
    assert [span.kind for span in document.spans] == ["digital", "ocr", "digital"]
    assert document.canonical_payload()["ocr"] == {
        "pipeline_identity": "ocr-test-v1",
        "attempted_page_numbers": [2],
        "recovered_page_numbers": [2],
    }


def test_pdf_ocr_fallback_is_not_called_when_every_page_has_digital_text() -> None:
    fallback = FakeOcrFallback({})

    document = PdfExtractor(ocr_fallback=fallback).extract(
        data=_digital_pdf("First page", "Second page")
    )

    assert fallback.calls == []
    assert document.parser_name == "pypdf"
    assert document.ocr is None
    assert "ocr" not in document.canonical_payload()


def test_pdf_ocr_fallback_rejects_incomplete_page_results() -> None:
    class WrongPageFallback:
        artifact_identity = "ocr-test-v1"

        def extract(
            self,
            *,
            pdf_data: bytes,
            page_numbers: Sequence[int],
        ) -> tuple[OcrPageResult, ...]:
            del pdf_data, page_numbers
            return (OcrPageResult(page_number=99, text="wrong page"),)

    with pytest.raises(InvalidProvenanceError, match="exactly the requested pages"):
        PdfExtractor(ocr_fallback=WrongPageFallback()).extract(
            data=_digital_pdf("First page", "", "Third page")
        )


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
