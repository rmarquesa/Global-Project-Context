from __future__ import annotations

from gpc.indexer import chunk_text


def test_chunk_text_splits_large_paragraphs_and_uses_stable_hashes() -> None:
    text = "A" * 12

    first = chunk_text(
        text, relative_path="README.md", chunk_type="documentation", max_chars=5
    )
    second = chunk_text(
        text, relative_path="README.md", chunk_type="documentation", max_chars=5
    )

    assert [chunk.content for chunk in first] == ["A" * 5, "A" * 5, "A" * 2]
    assert [chunk.content_hash for chunk in first] == [
        chunk.content_hash for chunk in second
    ]
    assert [chunk.title for chunk in first] == [
        "README.md#0",
        "README.md#1",
        "README.md#2",
    ]


def test_chunk_text_preserves_paragraph_boundaries_when_budget_allows() -> None:
    text = "First paragraph.\n\nSecond paragraph."

    chunks = chunk_text(
        text, relative_path="docs/a.md", chunk_type="documentation", max_chars=100
    )

    assert len(chunks) == 1
    assert "First paragraph" in chunks[0].content
    assert "Second paragraph" in chunks[0].content
    assert chunks[0].token_count == len(chunks[0].content.split())


def test_chunk_text_returns_empty_for_blank_input() -> None:
    assert (
        chunk_text(
            "\n\n", relative_path="empty.md", chunk_type="documentation", max_chars=100
        )
        == []
    )
