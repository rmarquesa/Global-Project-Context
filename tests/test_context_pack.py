from __future__ import annotations

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
