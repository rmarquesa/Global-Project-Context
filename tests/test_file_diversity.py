"""Post-fusion selection contracts, using only synthetic hydrated candidates."""

from dataclasses import replace
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gpc import search
from gpc.cli import build_parser, cmd_eval_search
from gpc.eval_candidates import capture_sha256, replay_paths
from gpc.eval_corpus import EvalCorpusSnapshot
from gpc.retrieval import select_file_indices
import gpc.search_eval as evaluation


@pytest.fixture
def pool(monkeypatch):
    # A duplicate ID is counted once per channel; an unhydrated foreign ID must
    # never appear in captures or returned selections. Paths are repo-qualified.
    rows = [
        dict(
            id=str(i),
            relative_path=path,
            repo_slug=repo,
            title="private-title",
            content="private-content",
            chunk_type=kind,
            language="python",
            chunk_index=i,
            content_hash="private-hash",
        )
        for i, path, repo, kind in [
            (1, "main.py", "one", "function"),
            (2, "main.py", "one", "function"),
            (3, "main.py", "two", "function"),
            (4, "tests/test_main.py", "one", "test"),
            (5, "README.md", "one", "documentation"),
        ]
    ]
    semantic = Mock(
        return_value=[("1", 0.9), ("1", 0.8), ("foreign", 0.7)]
        + [(str(i), 0.6) for i in range(2, 6)]
    )
    lexical = Mock(return_value=[(str(i), 10.0) for i in range(1, 6)])
    hydrate = Mock(return_value=rows)
    resolve = Mock(return_value={"id": "project", "slug": "p"})
    monkeypatch.setattr(search, "resolve_project", resolve)
    monkeypatch.setattr(search, "_semantic_candidates", semantic)
    monkeypatch.setattr(search, "lexical_candidates", lexical)
    monkeypatch.setattr(search, "_fetch_chunks", hydrate)
    return SimpleNamespace(
        semantic=semantic, lexical=lexical, hydrate=hydrate, resolve=resolve, rows=rows
    )


def retrieve(*, trace=None, **options):
    return search.search_project_context(
        "synthetic", project="p", limit=3, candidate_trace=trace, **options
    )[1]


@pytest.mark.parametrize("strategy", ["legacy", "weighted"])
@pytest.mark.parametrize("task", search.SEARCH_TASKS)
def test_selection_preserves_full_ranking_scores_tasks_and_budget(pool, strategy, task):
    options = {"mode": "hybrid", "task": task, "lexical_strategy": strategy}
    old_trace, explicit_trace, selected_trace = {}, {}, {}
    old = retrieve(trace=old_trace, **options)
    explicit = retrieve(trace=explicit_trace, file_diversity="none", **options)
    assert explicit == old
    assert explicit_trace == old_trace
    assert json.dumps(explicit_trace) == json.dumps(old_trace)
    assert old_trace["version"] == (2 if strategy == "weighted" else 1)
    before = [
        backend.call_count for backend in (pool.semantic, pool.lexical, pool.hydrate)
    ]
    selected = retrieve(trace=selected_trace, file_diversity="one-per-file", **options)
    assert [
        backend.call_count for backend in (pool.semantic, pool.lexical, pool.hydrate)
    ] == [count + 1 for count in before]
    assert pool.semantic.call_args.kwargs["limit"] == 9
    assert pool.lexical.call_args.kwargs["limit"] == 9
    assert pool.hydrate.call_args.args == (["1", "foreign", "2", "3", "4", "5"],)
    assert pool.hydrate.call_args.kwargs == {
        "project_id": "project",
        "repo_slugs": None,
    }
    assert selected_trace["ranked_candidates"] == old_trace["ranked_candidates"]
    assert selected_trace["channels"] == old_trace["channels"]
    assert selected_trace["selected_paths"] == replay_paths(old_trace, 1)
    assert selected_trace["selected_chunk_ids"] == [r.chunk_id for r in selected]
    # Compare complete result objects against the unselected ranking, not just IDs.
    full = search.search_project_context("synthetic", project="p", limit=5, **options)[
        1
    ]
    assert selected == [
        next(r for r in full if r.chunk_id == s.chunk_id) for s in selected
    ]
    if task == "general":
        assert selected_trace["selected_paths"][:2] == ["one:main.py", "two:main.py"]
        assert [r.chunk_id for r in selected] != [r.chunk_id for r in old]


@pytest.mark.parametrize("strategy", ["legacy", "weighted"])
def test_capture_v3_safe_explicit_selection_hash_and_policy(pool, strategy):
    baseline, traced = {}, {}
    options = {"mode": "hybrid", "lexical_strategy": strategy}
    retrieve(trace=baseline, **options)
    found = retrieve(trace=traced, file_diversity="one-per-file", **options)
    assert traced["version"] == 3
    assert traced["file_diversity"] == "one-per-file"
    assert traced["selection_policy_version"] == "one-per-file-v1"
    assert traced["selected_chunk_ids"] == [r.chunk_id for r in found]
    assert len(traced["ranked_candidates"]) == 5
    assert traced["selected_paths"] == [
        "one:main.py",
        "two:main.py",
        "one:tests/test_main.py",
    ]
    if strategy == "weighted":
        for key in ("lexical_strategy", "lexical_policy_version", "lexical_tie_policy"):
            assert traced[key] == baseline[key]
    else:
        assert not any(key.startswith("lexical_") for key in traced)
    assert traced["channels"]["semantic"]["duplicates_returned"] == 1
    assert traced["channels"]["semantic"]["dropped"] == 1
    encoded = json.dumps(traced)
    assert all(
        value not in encoded
        for value in ("foreign", "private-content", "private-title", "private-hash")
    )
    assert capture_sha256(traced) != capture_sha256(baseline)
    assert capture_sha256(traced) != capture_sha256(
        {**traced, "selected_chunk_ids": []}
    )
    assert capture_sha256(traced) != capture_sha256(
        {**traced, "selection_policy_version": "other"}
    )
    assert replay_paths(traced, 2) == replay_paths(baseline, 2)


def test_short_empty_failed_captures_no_backfill(pool):
    pool.hydrate.return_value = pool.rows[:2]
    trace = {"stale": True}
    found = retrieve(mode="hybrid", file_diversity="one-per-file", trace=trace)
    assert [r.chunk_id for r in found] == ["1"]
    assert trace["selected_chunk_ids"] == ["1"]
    assert len(trace["ranked_candidates"]) == 2
    pool.semantic.return_value = []
    pool.lexical.return_value = []
    empty = retrieve(mode="hybrid", file_diversity="one-per-file", trace=trace)
    assert empty == [] and trace["selected_chunk_ids"] == []
    assert trace["version"] == 3 and trace["ranked_candidates"] == []
    assert pool.hydrate.call_count == 1
    pool.semantic.side_effect = RuntimeError("failed backend")
    with pytest.raises(RuntimeError, match="failed backend"):
        retrieve(mode="hybrid", file_diversity="one-per-file", trace=trace)
    assert trace == {}


@pytest.mark.parametrize("failure", ["semantic", "lexical", "hydrate"])
def test_backend_failure_clears_capture(pool, failure):
    getattr(pool, failure).side_effect = RuntimeError("failed backend")
    trace = {"stale": True}
    with pytest.raises(RuntimeError, match="failed backend"):
        retrieve(mode="hybrid", file_diversity="one-per-file", trace=trace)
    assert trace == {}


def test_empty_authoritative_hydration_keeps_empty_safe_v3_capture(pool):
    pool.hydrate.return_value = []
    trace = {}
    assert retrieve(mode="hybrid", file_diversity="one-per-file", trace=trace) == []
    assert trace["version"] == 3
    assert trace["selected_chunk_ids"] == trace["selected_paths"] == []
    assert trace["ranked_candidates"] == []
    assert trace["channels"]["semantic"]["candidates"] == []
    assert trace["channels"]["semantic"]["dropped"] == 6


@pytest.mark.parametrize("mode", ["semantic", "lexical"])
def test_default_single_channel_unchanged_and_nondefault_fails_early(pool, mode):
    old_trace, trace = {}, {}
    assert retrieve(mode=mode, trace=old_trace) == retrieve(
        mode=mode, trace=trace, file_diversity="none"
    )
    assert trace == old_trace and trace["version"] == 1
    pool.resolve.reset_mock()
    with pytest.raises(ValueError, match="requires hybrid"):
        retrieve(mode=mode, file_diversity="one-per-file")
    pool.resolve.assert_not_called()


@pytest.mark.parametrize("policy", ["unknown", "", None, True, []])
def test_invalid_options_fail_before_resolution_fixture_or_backend(
    pool, monkeypatch, policy
):
    load = Mock(side_effect=AssertionError("must not read fixture"))
    monkeypatch.setattr(evaluation, "load_search_eval_fixture", load)
    for mode in search.SEARCH_MODES:
        with pytest.raises(ValueError, match="file_diversity"):
            retrieve(mode=mode, file_diversity=policy)
        with pytest.raises(ValueError, match="file_diversity"):
            evaluation.run_search_eval("p", "unused", mode=mode, file_diversity=policy)
    with pytest.raises(ValueError, match="file_diversity"):
        evaluation.compare_search_eval("p", "unused", file_diversity=policy)
    pool.resolve.assert_not_called()
    pool.semantic.assert_not_called()
    pool.lexical.assert_not_called()
    pool.hydrate.assert_not_called()
    load.assert_not_called()


@pytest.mark.parametrize("k, cap", [(0, 1), (True, 1), (1, True), (1, 0), (1, 3)])
def test_internal_selector_validates_bounds(k, cap):
    with pytest.raises(ValueError):
        select_file_indices(["one:a"], k=k, cap=cap)


@pytest.fixture
def eval_fixture(tmp_path, monkeypatch, pool):
    fixture = tmp_path / "synthetic.yaml"
    fixture.write_text(
        "queries:\n"
        "  - query: onde fica o código?\n"
        "    expected_paths:\n"
        "      - main.py\n"
        "      - tests/test_main.py\n"
        "      - absent.py\n"
        "      - main.py\n"
        "  - query: onde fica o código?\n"
        "    expected_paths:\n"
        "      - two:main.py\n"
        "  - query: where is the code?\n"
        "    expected_paths:\n"
        "      - README.md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluation, "active_embedding_model", lambda: "synthetic")
    monkeypatch.setattr(evaluation, "embedding_dimension", lambda: 2)
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: 0)
    snapshot = EvalCorpusSnapshot("p", "p", (("r", "one"), ("r2", "two")), ())
    monkeypatch.setattr(evaluation, "load_eval_corpus", Mock(return_value=snapshot))
    return fixture


@pytest.mark.parametrize("without_yaml", [False, True])
@pytest.mark.parametrize("strategy", ["legacy", "weighted"])
def test_compare_only_selects_hybrid_preserves_pool_baseline_and_denominators(
    eval_fixture, pool, monkeypatch, without_yaml, strategy
):
    if without_yaml:
        monkeypatch.setitem(sys.modules, "yaml", None)
    options = dict(
        k=3, include_lexical=True, analyze_candidates=True, lexical_strategy=strategy
    )
    old = evaluation.compare_search_eval("p", eval_fixture, **options)
    selected = evaluation.compare_search_eval(
        "p", eval_fixture, file_diversity="one-per-file", **options
    )
    assert selected["baseline"] == old["baseline"]
    assert selected["lexical"] == old["lexical"]
    candidate = selected["candidate"]
    assert candidate["file_diversity"] == "one-per-file"
    assert candidate["selection_policy_version"] == "one-per-file-v1"
    assert (
        candidate["summary"]["expected_paths"]
        == old["candidate"]["summary"]["expected_paths"]
        == 5
    )
    assert (
        candidate["candidate_analysis"]["candidate_pool"]
        == old["candidate"]["candidate_analysis"]["candidate_pool"]
    )
    for i, (actual, previous) in enumerate(
        zip(candidate["results"], old["candidate"]["results"])
    ):
        analysis = actual["candidate_analysis"]
        assert analysis["case_index"] == actual["diagnostics"]["case_index"] == i
        assert analysis["capture"]["selected_paths"] == actual["result_paths"]
        assert analysis["baseline"]["result_count"] == len(actual["result_paths"])
        assert analysis["baseline"]["unique_files"] == 3
        assert analysis["baseline"]["repeated_paths"] == {}
        assert (
            analysis["candidate_pool"]
            == previous["candidate_analysis"]["candidate_pool"]
        )
        assert analysis["caps"] == previous["candidate_analysis"]["caps"]
    assert candidate["results"][0]["candidate_analysis"]["baseline"]["hits"] == 2
    assert old["candidate"]["results"][0]["candidate_analysis"]["baseline"]["hits"] == 1


def test_without_analysis_metadata_and_cli_json_human_provenance(
    eval_fixture, pool, capsys
):
    pool.hydrate.return_value = pool.rows[:2]
    flags = [
        "eval-search",
        "--project",
        "p",
        "--fixture",
        str(eval_fixture),
        "--mode",
        "hybrid",
        "--file-diversity",
        "one-per-file",
        "--k",
        "3",
    ]
    assert cmd_eval_search(build_parser().parse_args(flags + ["--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["file_diversity"] == "one-per-file"
    assert "candidate_analysis" not in payload and "diagnostics" not in payload
    assert "lexical_strategy" not in payload
    assert cmd_eval_search(build_parser().parse_args(flags)) == 0
    text = capsys.readouterr().out
    assert "selection_policy_version=one-per-file-v1" in text
    assert "result_count=3 short_results=3" in text
    assert "No backfill, new query, or larger candidate budget" in text


@pytest.mark.parametrize("mode", ["semantic", "lexical"])
def test_eval_and_cli_reject_selection_for_nonhybrid_before_fixture(monkeypatch, mode):
    load = Mock(side_effect=AssertionError("must not read fixture"))
    monkeypatch.setattr(evaluation, "load_search_eval_fixture", load)
    with pytest.raises(ValueError, match="requires hybrid"):
        evaluation.run_search_eval(
            "p", "unused", mode=mode, file_diversity="one-per-file"
        )
    args = build_parser().parse_args(
        [
            "eval-search",
            "--project",
            "p",
            "--fixture",
            "unused",
            "--mode",
            mode,
            "--file-diversity",
            "one-per-file",
        ]
    )
    with pytest.raises(ValueError, match="requires hybrid"):
        cmd_eval_search(args)
    load.assert_not_called()


def test_compare_all_cli_json_and_human_selection_only_on_candidate(
    eval_fixture, capsys
):
    flags = [
        "eval-search",
        "--project",
        "p",
        "--fixture",
        str(eval_fixture),
        "--compare-all",
        "--lexical-strategy",
        "weighted",
        "--file-diversity",
        "one-per-file",
        "--analyze-candidates",
        "--k",
        "3",
    ]
    assert cmd_eval_search(build_parser().parse_args(flags + ["--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate"]["file_diversity"] == "one-per-file"
    assert payload["candidate"]["lexical_strategy"] == "weighted"
    assert "file_diversity" not in payload["baseline"]
    assert "file_diversity" not in payload["lexical"]
    assert "lexical_strategy" not in payload["baseline"]
    assert payload["lexical"]["lexical_strategy"] == "weighted"
    assert cmd_eval_search(build_parser().parse_args(flags)) == 0
    text = capsys.readouterr().out
    assert text.count("selection_policy_version=one-per-file-v1") == 1
    assert "baseline mode=semantic" in text
    assert "candidate mode=hybrid" in text
    assert "lexical mode=lexical" in text


def test_default_run_passes_no_new_kwargs_to_existing_mock(eval_fixture, monkeypatch):
    backend = Mock(return_value=({}, []))
    monkeypatch.setattr(evaluation, "search_project_context", backend)
    report = evaluation.run_search_eval("p", eval_fixture, k=3)
    assert all("file_diversity" not in call.kwargs for call in backend.call_args_list)
    assert "file_diversity" not in report.to_dict()
    assert "selection_policy_version" not in report.to_dict()
    assert replace(report, file_diversity="none").to_dict() == report.to_dict()
