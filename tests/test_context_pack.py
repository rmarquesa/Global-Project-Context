from __future__ import annotations

import re

import pytest

from gpc.context_pack import ContextPackChunk, render_context_pack


def test_render_context_pack_respects_budget_and_adds_citations() -> None:
    chunks = [
        ContextPackChunk(path="README.md", title="Intro", content="A" * 20, score=0.9),
        ContextPackChunk(
            path="docs/architecture.md", title=None, content="B" * 700, score=0.8
        ),
    ]

    pack = render_context_pack("architecture", chunks, max_chars=160)

    assert pack.included_chunks >= 1
    assert pack.truncated is True
    assert "# Context Pack" in pack.markdown
    assert "[1] README.md" in pack.markdown
    assert "## Citations" in pack.markdown
    assert "## Validation Commands" in pack.markdown
    assert pack.token_estimate > 0


def test_render_context_pack_handles_empty_results() -> None:
    pack = render_context_pack("missing", [], max_chars=200)

    assert pack.included_chunks == 0
    assert pack.truncated is False
    assert "No chunks retrieved" in pack.markdown


def assert_pack_consistent(pack) -> None:
    assert len(pack.markdown) <= pack.max_chars
    sections = re.findall(r"^## \[(\d+)\] ", pack.markdown, re.MULTILINE)
    citation_lines = re.findall(r"^\[(\d+)\] ", pack.markdown, re.MULTILINE)
    assert sections == citation_lines == [str(c["index"]) for c in pack.citations]
    assert pack.included_chunks == len(pack.citations)
    assert pack.markdown.endswith("\n")


def test_large_content_and_graph_preserve_real_evidence() -> None:
    chunk = ContextPackChunk(
        "README.md", "Intro", "REAL " * 2000, 0.9, graph_notes="GRAPH " * 2000
    )
    pack = render_context_pack("q", [chunk], max_chars=1000)
    assert_pack_consistent(pack)
    assert pack.included_chunks == 1
    assert pack.truncated
    assert "REAL " in pack.markdown
    assert "GRAPH" not in pack.markdown
    assert "Truncated to fit" in pack.markdown


def test_long_query_with_no_results_is_truncated() -> None:
    pack = render_context_pack("Q" * 2000, [], max_chars=500)
    assert_pack_consistent(pack)
    assert pack.truncated
    assert pack.citations == []
    assert "No chunks retrieved" in pack.markdown


@pytest.mark.parametrize("field", ["path", "title", "repo_slug"])
def test_unrepresentable_source_metadata_omits_entire_chunk(field) -> None:
    fields = dict(
        path="README.md", title="Intro", content="useful", score=0.9, repo_slug="api"
    )
    fields[field] = "X" * 10000
    pack = render_context_pack("q", [ContextPackChunk(**fields)], max_chars=500)
    assert_pack_consistent(pack)
    assert pack.truncated
    assert pack.included_chunks == 0
    assert pack.citations == []
    assert "No chunks included" in pack.markdown


def test_oversized_metadata_does_not_mutate_input_lists() -> None:
    warnings = ["W" * 10000]
    commands = ["C" * 10000]
    pack = render_context_pack(
        "Q" * 10000,
        [],
        project_slug="P" * 10000,
        warnings=warnings,
        validation_commands=commands,
        max_chars=500,
    )
    assert_pack_consistent(pack)
    assert pack.truncated
    assert warnings == ["W" * 10000]
    assert commands == ["C" * 10000]
    assert pack.warnings == warnings
    assert pack.validation_commands == commands
    assert pack.warnings is not warnings
    assert pack.validation_commands is not commands


@pytest.mark.parametrize(
    "field", ["query", "project_slug", "warnings", "validation_commands"]
)
def test_each_oversized_pack_metadata_field_is_bounded(field) -> None:
    kwargs = {
        "query": "q",
        "project_slug": "p",
        "warnings": ["warning"],
        "validation_commands": ["check"],
    }
    kwargs[field] = (
        ["X" * 10000] if field in {"warnings", "validation_commands"} else "X" * 10000
    )
    pack = render_context_pack(chunks=[], max_chars=500, **kwargs)
    assert_pack_consistent(pack)
    assert pack.truncated


def test_many_chunks_count_only_included_sources() -> None:
    chunks = [
        ContextPackChunk(f"{i}.md", None, "evidence" * 10, 0.8) for i in range(1000)
    ]
    pack = render_context_pack("q", chunks, max_chars=1500)
    assert_pack_consistent(pack)
    assert 0 < pack.included_chunks < len(chunks)
    assert pack.truncated


def test_same_relative_path_is_repository_qualified_in_rendered_text() -> None:
    pack = render_context_pack(
        "q",
        [
            ContextPackChunk("README.md", None, "api evidence", 0.9, repo_slug="api"),
            ContextPackChunk("README.md", None, "web evidence", 0.8, repo_slug="web"),
        ],
    )
    assert_pack_consistent(pack)
    assert "[1] README.md (repo=api)" in pack.markdown
    assert "[2] README.md (repo=web)" in pack.markdown
    assert not pack.truncated


def test_long_graph_only_is_explicitly_omitted() -> None:
    pack = render_context_pack(
        "q",
        [
            ContextPackChunk(
                "a.md", None, "keep this evidence", 0.9, graph_notes="g" * 10000
            )
        ],
        max_chars=1000,
    )
    assert_pack_consistent(pack)
    assert "keep this evidence" in pack.markdown
    assert "Truncated to fit" in pack.markdown
    assert pack.truncated
