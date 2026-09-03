"""Proof for deterministic, provenance-bearing WikiRAG page skeletons."""

import pytest
from pydantic import ValidationError

from openwikirag.application.extraction import (
    MarkdownExtractor,
    NormalizedDocument,
    PlainTextExtractor,
)
from openwikirag.application.metadata import (
    DeterministicMetadataExtractor,
    DocumentMetadata,
    MetadataEvidence,
)
from openwikirag.application.wiki import (
    WikiDefinition,
    WikiPageBuilder,
    WikiPageBuildError,
    WikiReference,
    WikiSection,
)


def _metadata_for(text: bytes) -> tuple[NormalizedDocument, DocumentMetadata]:
    document = MarkdownExtractor().extract(text)
    metadata = DeterministicMetadataExtractor().extract(document=document)
    return document, metadata


def test_page_builder_is_deterministic_and_preserves_structural_provenance() -> None:
    document, metadata = _metadata_for(
        b"# Overview\nIntroductory text.\n## Scope\nThis is a guide for the team.\n"
    )
    builder = WikiPageBuilder()

    first = builder.build(document=document, metadata=metadata)
    second = builder.build(document=document, metadata=metadata)

    assert first.title == "Overview"
    assert first.language == "en"
    assert first.source_type == "markdown"
    assert first.source_artifact_checksum == document.checksum_sha256
    assert first.metadata_checksum == metadata.checksum_sha256
    assert first.review_status == "draft"
    assert [section.title for section in first.sections] == ["Overview", "Scope"]
    assert [section.level for section in first.sections] == [1, 2]
    assert [section.section_path for section in first.sections] == [
        ("Overview",),
        ("Overview", "Scope"),
    ]
    assert all(section.section_id.startswith("section-") for section in first.sections)
    assert first.sections[0].heading.raw_text == "# Overview\n"
    assert first.definitions == ()
    assert first.references == ()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.checksum_sha256 == second.checksum_sha256


def test_page_builder_allows_headingless_documents_and_typed_future_fields() -> None:
    document, metadata = _metadata_for(b"This is a short guide for the platform team.")
    page = WikiPageBuilder().build(document=document, metadata=metadata)

    assert page.sections == ()
    assert page.title == "This is a short guide for the platform team."

    evidence = MetadataEvidence(
        value="platform",
        raw_text="platform",
        normalized_start_char=document.text.index("platform"),
        normalized_end_char=document.text.index("platform") + len("platform"),
    )
    definition = WikiDefinition(
        term="platform",
        definition="A shared system.",
        evidence=(evidence,),
    )
    reference = WikiReference(
        label="Platform guide",
        target="https://example.test/platform",
        evidence=(evidence,),
    )

    assert definition.evidence[0].value == "platform"
    assert reference.target.endswith("platform")


def test_page_builder_rejects_mismatched_source_artifacts_and_evidence() -> None:
    document, metadata = _metadata_for(b"# First\nThis is a guide.")
    different_document, _ = _metadata_for(b"# Second\nThis is a guide.")

    with pytest.raises(WikiPageBuildError, match="different source artifact"):
        WikiPageBuilder().build(document=different_document, metadata=metadata)

    invalid_evidence = MetadataEvidence(
        value="wrong",
        raw_text="wrong",
        normalized_start_char=0,
        normalized_end_char=5,
    )
    invalid_metadata = metadata.model_copy(update={"title_evidence": invalid_evidence})
    with pytest.raises(WikiPageBuildError, match="does not match"):
        WikiPageBuilder().build(document=document, metadata=invalid_metadata)

    text_document = PlainTextExtractor().extract(b"This is text.")
    text_metadata = DeterministicMetadataExtractor().extract(document=text_document)
    with pytest.raises(WikiPageBuildError, match="source types differ"):
        WikiPageBuilder().build(document=text_document, metadata=metadata)
    assert text_metadata.source_type == "text"


def test_wiki_section_validates_heading_contract() -> None:
    document, metadata = _metadata_for(b"# Overview\nBody.")
    heading = metadata.headings[0]

    with pytest.raises(ValidationError, match="title must match"):
        WikiSection(
            section_id="section-" + "a" * 64,
            title="Wrong",
            level=heading.level,
            section_path=heading.section_path,
            heading=heading,
        )


def test_future_definition_and_reference_fields_require_evidence() -> None:
    with pytest.raises(ValidationError):
        WikiDefinition(term="term", definition="definition", evidence=())
    with pytest.raises(ValidationError):
        WikiReference(label="label", target="target", evidence=())
