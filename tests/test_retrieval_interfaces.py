from __future__ import annotations

from dataclasses import replace
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gpc import cli, context_pack, mcp_observability, mcp_server, search_eval
from gpc.search import SEARCH_MODES, SearchResult

PROJECT = {"id": "p-id", "slug": "p", "name": "P", "root_path": "/p"}


def result(repo="api", content="evidence", **kwargs):
    return SearchResult(
        score=0.032,
        chunk_id=f"chunk-{repo}",
        relative_path="README.md",
        title=None,
        content=content,
        chunk_type="documentation",
        language="markdown",
        repo_slug=repo,
        retrieval_sources=("semantic", "lexical"),
        ranking_signals=("rrf:k=60", "task:review:boost=1.05"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "q"],
        ["context-pack", "q", "--output", "pack.md"],
        ["eval-search", "--project", "p", "--fixture", "fixture.yml"],
    ],
)
def test_retrieval_parser_defaults_and_choices(argv):
    args = cli.build_parser().parse_args(argv)
    assert (args.mode, args.task) == ("semantic", "general")
    for mode in SEARCH_MODES:
        args = cli.build_parser().parse_args(argv + ["--mode", mode, "--task", "test"])
        assert (args.mode, args.task) == (mode, "test")
    for flag in ("--mode", "--task"):
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(argv + [flag, "invalid"])
        assert exc.value.code == 2


def test_search_parser_repeatable_repos_and_compare_exclusivity():
    args = cli.build_parser().parse_args(
        ["search", "q", "--repo", "api", "--repo", "web"]
    )
    assert args.repo == ["api", "web"]
    argv = ["eval-search", "--project", "p", "--fixture", "f", "--compare"]
    assert cli.build_parser().parse_args(argv).compare
    for mode in SEARCH_MODES:
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(argv + ["--mode", mode])
        assert exc.value.code == 2


def test_cmd_search_forwards_options_and_displays_repo_and_raw_provenance(
    monkeypatch, capsys
):
    retrieve = MagicMock(return_value=(PROJECT, [result(), result("web")]))
    monkeypatch.setattr(cli, "search_project_context", retrieve)
    argv = (
        "search q --project p --cwd /caller --limit 2 --content-chars 4 "
        "--repo api --repo web --mode hybrid --task review"
    ).split()
    assert cli.main(argv) == 0
    retrieve.assert_called_once_with(
        "q",
        project="p",
        cwd="/caller",
        limit=2,
        repo=["api", "web"],
        mode="hybrid",
        task="review",
    )
    text = capsys.readouterr().out
    assert "repo=api path=README.md" in text and "repo=web path=README.md" in text
    assert "score=0.0320" in text
    assert "sources=semantic,lexical" in text
    assert "signals=rrf:k=60,task:review:boost=1.05" in text
    assert "evid..." in text


def test_cmd_context_pack_forwards_options_and_writes_only_requested_path(
    monkeypatch, tmp_path, capsys
):
    pack = context_pack.render_context_pack("q", [])
    build = MagicMock(return_value=pack)
    monkeypatch.setattr(context_pack, "build_context_pack", build)
    output = tmp_path / "packs" / "context.md"
    argv = (
        "context-pack q --project p --cwd /caller --repo api --max-chunks 3 "
        "--max-chars 900 --include-graph --mode lexical --task debug --output"
    ).split() + [str(output)]
    assert cli.main(argv) == 0
    build.assert_called_once_with(
        "q",
        project="p",
        cwd="/caller",
        repo=["api"],
        max_chunks=3,
        max_chars=900,
        include_graph=True,
        mode="lexical",
        task="debug",
    )
    assert output.read_text() == pack.markdown
    assert f"context_pack={output}" in capsys.readouterr().out


def eval_report(mode="lexical"):
    data = {
        "mode": mode,
        "task": "test",
        "summary": {
            "queries": 1,
            "hits": 1,
            "expected_paths": 2,
            "recall_at_k": 0.5,
            "query_hit_rate": 1.0,
            "mrr_at_k": 0.5,
            "ndcg_at_k": 0.6,
            "mean_latency_ms": 2.0,
            "p95_latency_ms": 2.0,
        },
    }
    return SimpleNamespace(
        to_dict=lambda: data,
        summary=data["summary"],
        results=[
            SimpleNamespace(
                query="q", hit_paths=["README.md"], missing_expected_paths=["a.py"]
            )
        ],
    )


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("compare", [False, True])
def test_cmd_eval_single_and_compare_output_and_dispatch(
    monkeypatch, capsys, as_json, compare
):
    report = eval_report()
    comparison = {
        "baseline": eval_report("semantic").to_dict(),
        "candidate": eval_report("hybrid").to_dict(),
        "delta": {"recall_at_k": 0.25, "mean_latency_ms": -1.0},
    }
    run = MagicMock(return_value=report)
    compare_run = MagicMock(return_value=comparison)
    monkeypatch.setattr(search_eval, "run_search_eval", run)
    monkeypatch.setattr(search_eval, "compare_search_eval", compare_run)
    argv = "eval-search --project p --fixture f --k 3 --task test".split()
    argv += ["--compare"] if compare else ["--mode", "lexical"]
    if as_json:
        argv += ["--json"]
    assert cli.main(argv) == 0
    text = capsys.readouterr().out
    if compare:
        compare_run.assert_called_once_with("p", "f", k=3, task="test")
        run.assert_not_called()
        if as_json:
            assert json.loads(text) == comparison
        else:
            assert "baseline mode=semantic" in text and "candidate mode=hybrid" in text
            assert "delta recall_at_k=+0.250 mean_latency_ms=-1.000" in text
    else:
        run.assert_called_once_with("p", "f", k=3, mode="lexical", task="test")
        compare_run.assert_not_called()
        if as_json:
            assert json.loads(text) == report.to_dict()
        else:
            assert "recall_at_3=0.500 mode=lexical task=test" in text
            assert "mrr_at_k=0.500" in text and "mean_latency_ms=2.000" in text
            assert "missing=['a.py']" in text


@pytest.mark.parametrize("command", ["search", "context-pack", "eval-search"])
def test_cli_expected_validation_failures_are_nonzero_and_clear(
    monkeypatch, capsys, command
):
    if command == "search":
        monkeypatch.setattr(
            cli,
            "search_project_context",
            MagicMock(side_effect=ValueError("invalid limit")),
        )
        argv = [command, "q"]
    elif command == "context-pack":
        monkeypatch.setattr(
            context_pack,
            "build_context_pack",
            MagicMock(side_effect=ValueError("invalid query")),
        )
        argv = [command, "q", "--output", "never-written.md"]
    else:
        monkeypatch.setattr(
            search_eval,
            "run_search_eval",
            MagicMock(side_effect=FileNotFoundError("missing fixture")),
        )
        argv = [command, "--project", "p", "--fixture", "missing"]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.startswith("gpc: ")


@pytest.fixture
def no_mcp_store_writes(monkeypatch):
    log = MagicMock(return_value=None)
    samples = MagicMock()
    monkeypatch.setattr(mcp_observability, "_write_log", log)
    monkeypatch.setattr(mcp_observability, "_write_token_savings_sample", samples)
    return log, samples


@pytest.mark.parametrize("mode", SEARCH_MODES)
@pytest.mark.parametrize("tool", ["search", "context"])
def test_mcp_public_tools_forward_options_and_serialize_provenance(
    monkeypatch, no_mcp_store_writes, mode, tool
):
    backend = MagicMock(
        return_value=(
            (PROJECT, [result()])
            if tool == "search"
            else (PROJECT, [result()], "bounded context")
        )
    )
    symbol = "search_project_context" if tool == "search" else "compose_project_context"
    monkeypatch.setattr(mcp_server, symbol, backend)
    common = dict(project="p", cwd="/caller", repo=["api"], mode=mode, task="review")
    if tool == "search":
        payload = mcp_server.mcp_search("q", limit=200, content_chars=1, **common)
        backend.assert_called_once_with("q", limit=20, **common)
        items = payload["results"]
        assert items[0]["content"] == "evidence"
    else:
        payload = mcp_server.mcp_context(
            "q",
            max_chunks=3,
            max_chars=700,
            include_graph=True,
            graph_min_confidence="INFERRED",
            **common,
        )
        backend.assert_called_once_with(
            "q",
            max_chunks=3,
            max_chars=700,
            include_graph=True,
            graph_min_confidence="INFERRED",
            **common,
        )
        assert payload["context"] == "bounded context"
        items = payload["sources"]
        assert "content" not in items[0]
    assert payload["ok"] and payload["mode"] == mode and payload["task"] == "review"
    assert payload["repo_filter"] == ["api"]
    assert items[0]["retrieval_sources"] == ["semantic", "lexical"]
    assert items[0]["ranking_signals"] == ["rrf:k=60", "task:review:boost=1.05"]
    assert json.loads(json.dumps(payload)) == payload
    log, samples = no_mcp_store_writes
    assert log.call_args.kwargs["success"] is True
    samples.assert_called_once()


@pytest.mark.parametrize("content_chars,expected_chars", [(1, 200), (99999, 8000)])
def test_mcp_search_preserves_content_bounds(
    monkeypatch, no_mcp_store_writes, content_chars, expected_chars
):
    backend = MagicMock(return_value=(PROJECT, [result(content="x" * 9000)]))
    monkeypatch.setattr(mcp_server, "search_project_context", backend)
    payload = mcp_server.mcp_search("q", content_chars=content_chars, mode="lexical")
    content = payload["results"][0]["content"]
    assert len(content) == expected_chars
    assert content.endswith("...")


@pytest.mark.parametrize("tool", ["search", "context"])
def test_mcp_old_positional_args_keep_semantic_general_defaults(
    monkeypatch, no_mcp_store_writes, tool
):
    fake = SimpleNamespace(
        score=0.9,
        chunk_id="old",
        relative_path="a",
        title=None,
        chunk_type="file",
        language=None,
        content="x",
    )
    backend = MagicMock(
        return_value=(PROJECT, [fake]) if tool == "search" else (PROJECT, [fake], "x")
    )
    monkeypatch.setattr(
        mcp_server,
        "search_project_context" if tool == "search" else "compose_project_context",
        backend,
    )
    if tool == "search":
        payload = mcp_server.mcp_search("q", "p", "/caller", 5, 1200, "api")
        item = payload["results"][0]
    else:
        payload = mcp_server.mcp_context(
            "q", "p", "/caller", 5, 6000, "api", False, "EXTRACTED"
        )
        item = payload["sources"][0]
    assert payload["ok"]
    assert payload["mode"] == backend.call_args.kwargs["mode"] == "semantic"
    assert payload["task"] == backend.call_args.kwargs["task"] == "general"
    assert item["retrieval_sources"] == item["ranking_signals"] == []


@pytest.mark.parametrize("tool", ["search", "context"])
@pytest.mark.parametrize("invalid", [{"mode": "bad"}, {"task": "bad"}])
def test_mcp_invalid_options_fail_before_backend(
    monkeypatch, no_mcp_store_writes, tool, invalid
):
    backend = MagicMock(side_effect=AssertionError("must not call backend"))
    monkeypatch.setattr(
        mcp_server,
        "search_project_context" if tool == "search" else "compose_project_context",
        backend,
    )
    payload = getattr(mcp_server, f"mcp_{tool}")("q", **invalid)
    assert payload["ok"] is False and payload["error"]["type"] == "ValueError"
    assert next(iter(invalid)) in payload["error"]["message"]
    backend.assert_not_called()
    assert no_mcp_store_writes[0].call_args.kwargs["success"] is False


def test_mcp_context_graph_confidence_validation_remains_intact(
    monkeypatch, no_mcp_store_writes
):
    backend = MagicMock()
    monkeypatch.setattr(mcp_server, "compose_project_context", backend)
    payload = mcp_server.mcp_context("q", mode="hybrid", graph_min_confidence="bad")
    assert payload["error"]["type"] == "ValueError"
    assert "graph_min_confidence" in payload["error"]["message"]
    backend.assert_not_called()


@pytest.mark.parametrize("tool", ["search", "context"])
def test_mcp_backend_failure_is_not_hidden(monkeypatch, no_mcp_store_writes, tool):
    backend = MagicMock(side_effect=RuntimeError("backend unavailable"))
    monkeypatch.setattr(
        mcp_server,
        "search_project_context" if tool == "search" else "compose_project_context",
        backend,
    )
    payload = getattr(mcp_server, f"mcp_{tool}")("q", mode="hybrid")
    assert payload == {
        "ok": False,
        "error": {"type": "RuntimeError", "message": "backend unavailable"},
    }
    backend.assert_called_once()


@pytest.mark.parametrize("mode", SEARCH_MODES)
def test_build_pack_retains_citation_provenance_and_one_search_graph_enrichment(
    monkeypatch, mode
):
    results = [result(), result("web")]
    search = MagicMock(return_value=(PROJECT, results))
    annotate = MagicMock(
        side_effect=lambda slug, rows: [
            replace(r, graph_notes="graph evidence") for r in rows
        ]
    )
    monkeypatch.setattr(context_pack, "search_project_context", search)
    monkeypatch.setattr(context_pack, "annotate_search_results", annotate)
    pack = context_pack.build_context_pack(
        "q",
        project="p",
        cwd="/caller",
        repo=["api", "web"],
        max_chunks=2,
        max_chars=1800,
        include_graph=True,
        mode=mode,
        task="review",
    )
    search.assert_called_once_with(
        "q",
        project="p",
        cwd="/caller",
        repo=["api", "web"],
        limit=2,
        mode=mode,
        task="review",
    )
    annotate.assert_called_once_with("p", results)
    assert len(pack.markdown) <= pack.max_chars == 1800
    assert pack.included_chunks == len(pack.citations) == 2
    assert not pack.truncated
    assert pack.markdown.count("graph evidence") == 2
    assert [c["repo_slug"] for c in pack.citations] == ["api", "web"]
    for citation in json.loads(json.dumps(pack.to_dict()))["citations"]:
        assert citation["retrieval_sources"] == ["semantic", "lexical"]
        assert citation["ranking_signals"] == ["rrf:k=60", "task:review:boost=1.05"]
    assert "rrf:k=60" not in pack.markdown


def test_pack_truncation_keeps_provenance_counts_and_citations_consistent(monkeypatch):
    search = MagicMock(
        return_value=(PROJECT, [result(content="evidence " * 2000), result("web")])
    )
    monkeypatch.setattr(context_pack, "search_project_context", search)
    pack = context_pack.build_context_pack(
        "q", max_chars=800, mode="hybrid", task="debug"
    )
    assert pack.truncated and len(pack.markdown) <= 800
    assert pack.included_chunks == len(pack.citations) == 1
    assert re.findall(r"^## \[(\d+)\] ", pack.markdown, re.MULTILINE) == ["1"]
    assert re.findall(r"^\[(\d+)\] ", pack.markdown, re.MULTILINE) == ["1"]
    assert pack.citations[0]["retrieval_sources"] == ["semantic", "lexical"]
    assert pack.citations[0]["ranking_signals"] == list(result().ranking_signals)
    search.assert_called_once()
