"""Proof for deterministic, heading-aware hierarchical chunks."""

import pytest

from openwikirag.application.chunking import (
    ChunkingConfig,
    ChunkingConfigurationError,
    ChunkingInputError,
    HierarchicalChunker,
)
from openwikirag.application.extraction import MarkdownExtractor, NormalizedDocument, SourceSpan


def test_chunks_are_heading_aware_and_children_reference_parents() -> None:
    document = MarkdownExtractor().extract(
        b"# Authentication\nJWT tokens verify identity.\n"
        b"## Rotation\nRefresh tokens are single use.\n"
        b"# Retrieval\nHybrid search combines lexical and semantic signals.\n"
    )

    result = HierarchicalChunker(
        ChunkingConfig(
            parent_max_tokens=50,
            parent_max_characters=500,
            child_max_tokens=8,
            child_max_characters=100,
            child_overlap_tokens=2,
        )
    ).chunk(document)

    assert [parent.section_path for parent in result.parents] == [
        ("Authentication",),
        ("Retrieval",),
    ]
    assert len(result.children) >= 2
    for parent in result.parents:
        children = tuple(
            child for child in result.children if child.parent_chunk_id == parent.chunk_id
        )
        assert children
        assert all(
            parent.normalized_start_char <= child.normalized_start_char
            and child.normalized_end_char <= parent.normalized_end_char
            for child in children
        )
        assert all(
            document.text[child.normalized_start_char : child.normalized_end_char] == child.text
            for child in children
        )


def test_chunk_ids_and_bytes_are_stable_for_the_same_input() -> None:
    document = MarkdownExtractor().extract(b"# Stable\nA deterministic retrieval unit.\n")
    config = ChunkingConfig(
        parent_max_tokens=20,
        parent_max_characters=200,
        child_max_tokens=5,
        child_max_characters=80,
        child_overlap_tokens=1,
    )

    first = HierarchicalChunker(config).chunk(document)
    second = HierarchicalChunker(config).chunk(document)

    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert [chunk.canonical_bytes() for chunk in first.chunks] == [
        chunk.canonical_bytes() for chunk in second.chunks
    ]
    assert all(
        chunk.source_artifact_checksum == document.checksum_sha256 for chunk in first.chunks
    )


def test_child_windows_respect_budgets_and_use_bounded_overlap() -> None:
    document = MarkdownExtractor().extract(
        b"# Retrieval\n" + b" ".join(f"term-{index}".encode() for index in range(30))
    )
    result = HierarchicalChunker(
        ChunkingConfig(
            parent_max_tokens=100,
            parent_max_characters=500,
            child_max_tokens=6,
            child_max_characters=60,
            child_overlap_tokens=2,
        )
    ).chunk(document)
    children = result.children

    assert len(children) > 2
    assert all(chunk.token_count <= 6 and chunk.character_count <= 60 for chunk in children)
    for previous, current in zip(children, children[1:], strict=False):
        if previous.parent_chunk_id == current.parent_chunk_id:
            assert current.normalized_start_char < previous.normalized_end_char
            assert current.normalized_end_char > previous.normalized_end_char
    assert all(chunk.text != result.parents[0].text for chunk in children)


def test_page_provenance_is_summarized_from_overlapping_source_spans() -> None:
    text = "alpha\nbeta\ngamma"
    document = NormalizedDocument(
        source_type="pdf",
        parser_name="test",
        parser_version="test-v1",
        text=text,
        spans=(
            SourceSpan(
                normalized_start_char=0,
                normalized_end_char=6,
                source_start_char=0,
                source_end_char=6,
                page_number=1,
                section_path=(),
            ),
            SourceSpan(
                normalized_start_char=6,
                normalized_end_char=len(text),
                source_start_char=6,
                source_end_char=len(text),
                page_number=2,
                section_path=(),
            ),
        ),
    )

    result = HierarchicalChunker(
        ChunkingConfig(
            parent_max_tokens=10,
            parent_max_characters=100,
            child_max_tokens=10,
            child_max_characters=100,
            child_overlap_tokens=0,
        )
    ).chunk(document)

    assert result.parents[0].page_start == 1
    assert result.parents[0].page_end == 2
    assert result.children[0].page_start == 1
    assert result.children[0].page_end == 2


def test_invalid_configuration_and_oversized_token_fail_before_output() -> None:
    with pytest.raises(ChunkingConfigurationError):
        ChunkingConfig(child_max_tokens=2, child_overlap_tokens=2)

    document = MarkdownExtractor().extract(b"# Oversized\nlongword\n")
    chunker = HierarchicalChunker(
        ChunkingConfig(
            parent_max_tokens=10,
            parent_max_characters=100,
            child_max_tokens=10,
            child_max_characters=3,
            child_overlap_tokens=0,
        )
    )
    with pytest.raises(ChunkingInputError):
        chunker.chunk(document)


def test_non_normalized_input_is_rejected() -> None:
    with pytest.raises(ChunkingInputError):
        HierarchicalChunker().chunk(object())  # type: ignore[arg-type]
