"""Finite, offline candidate fixtures: no service or embedding calls."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from gpc.eval_candidates import build_capture, capture_sha256, replay_paths
from gpc.eval_corpus import EvalCorpusSnapshot
from gpc.search import SearchResult
import gpc.search_eval as evaluation


def result(number, path, repo="one"):
    return SearchResult(
        1.0,
        str(number),
        path,
        "title-canary",
        "content-canary",
        "function",
        "python",
        repo,
    )


def capture(found, *, k=3, mode="hybrid", task="general"):
    return build_capture(
        {
            name: [(r.chunk_id, r.score) for r in found]
            for name in (("semantic", "lexical") if mode == "hybrid" else (mode,))
        },
        [(r.score, r) for r in found],
        mode=mode,
        task=task,
        k=k,
        candidate_limit=min(3 * k, 300),
    )


@pytest.mark.parametrize("cap", [1, 2])
def test_one_file_short_replay_never_refills(cap):
    found = [result(i, "main.py") for i in range(9)]
    traced = capture(found)
    assert replay_paths(traced, cap) == ["one:main.py"] * cap
    assert len(traced["ranked_candidates"]) == 9


def test_caps_filter_rank_order_and_keep_qualified_file_identity():
    found = [
        result(1, "main.py"),
        result(2, "main.py"),
        result(3, "main.py", "two"),
        result(4, "other.py"),
        result(5, "last.py"),
    ]
    traced = capture(found)
    assert replay_paths(traced, 1) == ["one:main.py", "two:main.py", "one:other.py"]
    assert replay_paths(traced, 2) == ["one:main.py", "one:main.py", "two:main.py"]
    with pytest.raises(ValueError):
        replay_paths(traced, 0)


def test_safe_capture_deterministic_hash_native_ranks_and_copied_values():
    found = [result(1, "a.py"), result(2, "b.py")]
    channels = {"lexical": [("1", 3.0), ("1", 99.0), ("foreign", 88.0), ("2", 2.0)]}
    traced = build_capture(
        channels,
        [(1, r) for r in found],
        mode="lexical",
        task="general",
        k=1,
        candidate_limit=3,
    )
    lexical = traced["channels"]["lexical"]
    assert [
        (r["rank"], r["backend_rank"], r["score"]) for r in lexical["candidates"]
    ] == [(1, 1, 3.0), (3, 4, 2.0)]
    assert lexical["dropped"] == 1 and lexical["duplicates_returned"] == 1
    encoded = json.dumps(traced, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert capture_sha256(traced) == hashlib.sha256(encoded.encode()).hexdigest()
    assert capture_sha256(traced) == capture_sha256(
        dict(reversed(list(traced.items())))
    )
    assert all(
        secret not in encoded
        for secret in ("foreign", "content-canary", "title-canary")
    )
    channels["lexical"][0] = ("1", 77)
    assert lexical["candidates"][0]["score"] == 3
    modified = json.loads(encoded)
    modified["k"] = 2
    assert capture_sha256(modified) != capture_sha256(traced)


def install_fixture(tmp_path, monkeypatch, found, *, cases=None, k=3):
    cases = cases or [
        evaluation.SearchEvalCase(
            "same", ["main.py", "one:other.py", "absent.py", "main.py"], repo="one"
        )
    ]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "query": c.query,
                        "expected_paths": c.expected_paths,
                        "repo": c.repo,
                    }
                    for c in cases
                ]
            }
        )
    )
    snapshot = EvalCorpusSnapshot(
        "p",
        "p",
        (("r1", "one"), ("r2", "two")),
        (("one", "main.py", "h", True), ("one", "other.py", "h2", True)),
    )
    load = Mock(return_value=snapshot)
    monkeypatch.setattr(evaluation, "load_eval_corpus", load)
    monkeypatch.setattr(evaluation, "active_embedding_model", lambda: "test")
    monkeypatch.setattr(evaluation, "embedding_dimension", lambda: 2)
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: 0)
    calls = []

    def search(query, **kw):
        calls.append(kw.copy())
        trace = kw.get("candidate_trace")
        if trace is not None:
            trace.update(
                capture(found, k=kw["limit"], mode=kw["mode"], task=kw["task"])
            )
        return {}, found[: kw["limit"]]

    monkeypatch.setattr(evaluation, "search_project_context", search)
    return fixture, snapshot, load, calls


def test_analysis_implies_diagnostics_preserves_baseline_and_label_denominators(
    tmp_path, monkeypatch
):
    found = [result(i, "main.py") for i in range(3)] + [result(4, "other.py")]
    fixture, snapshot, load, calls = install_fixture(tmp_path, monkeypatch, found)
    baseline = evaluation.run_search_eval("p", fixture, k=3)
    assert "candidate_trace" not in calls[-1]
    assert "candidate_analysis" not in baseline.to_dict()
    assert "candidate_analysis" not in baseline.results[0].to_dict()
    report = evaluation.run_search_eval("p", fixture, k=3, analyze_candidates=True)
    assert len(calls) == 2 and all(c["limit"] == 3 for c in calls)
    load.assert_called_once()
    assert report.summary == baseline.summary
    assert report.results[0].result_paths == baseline.results[0].result_paths
    row = report.results[0].candidate_analysis
    assert row["case_index"] == report.results[0].diagnostics["case_index"] == 0
    assert row["baseline"]["unique_files"] == 1
    assert row["baseline"]["repeated_paths"] == {"one:main.py": 3}
    assert row["candidate_pool"]["hit_paths"] == ["main.py", "one:other.py"]
    assert row["candidate_pool"]["missing_expected_paths"] == ["absent.py"]
    assert row["baseline"]["missing_expected_paths"] == ["one:other.py", "absent.py"]
    assert row["channels"]["semantic"]["unique_files"] == 2
    assert row["channels"]["semantic"]["expected_paths"] == 3
    assert row["lexical_ties"]["largest_tie"] == 0  # semantic mode
    for cap in (1, 2):
        replay = row["caps"][f"cap_{cap}"]
        assert replay["short_results"] == (cap == 1)
        assert replay["missing_expected_paths"] == ["absent.py"]
        assert "latency_ms" not in replay
        assert replay["summary"]["expected_paths"] == 3
        assert replay["summary"]["hits"] == 2
        assert all("latency" not in name for name in replay["summary"])
    assert report.candidate_analysis["candidate_pool"] == {
        "total_available": 4,
        "hits": 2,
        "expected_paths": 3,
        "recall": 2 / 3,
    }
    payload = report.to_dict()
    assert (
        payload["diagnostics"]["snapshot"]["fixture_sha256"]
        == hashlib.sha256(fixture.read_bytes()).hexdigest()
    )
    assert (
        payload["diagnostics"]["snapshot"]["metadata_sha256"]
        == snapshot.to_dict("")["metadata_sha256"]
    )
    serialized = json.dumps(payload, allow_nan=False)
    assert "content-canary" not in serialized and "title-canary" not in serialized
    assert serialized.count('"candidate_analysis"') == 2  # no recursive reports


def test_comparison_one_shared_fixture_snapshot_single_capture_per_case_and_macro_metrics(
    tmp_path, monkeypatch
):
    cases = [
        evaluation.SearchEvalCase(
            "same", ["main.py", "one:main.py", "absent.py"], repo="one"
        ),
        evaluation.SearchEvalCase("same", [], repo="two"),
    ]
    found = [result(1, "main.py"), result(2, "main.py"), result(3, "other.py")]
    fixture, snapshot, load, calls = install_fixture(
        tmp_path, monkeypatch, found, cases=cases
    )
    original = evaluation._load_fixture_snapshot
    read = Mock(side_effect=original)
    monkeypatch.setattr(evaluation, "_load_fixture_snapshot", read)
    original_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    load.side_effect = lambda *a: (fixture.write_text("queries: []"), snapshot)[1]
    comparison = evaluation.compare_search_eval(
        "p", fixture, k=3, analyze_candidates=True, include_lexical=True
    )
    load.assert_called_once()
    read.assert_called_once()
    assert len(calls) == 6
    assert [c["mode"] for c in calls] == [
        "semantic",
        "semantic",
        "hybrid",
        "hybrid",
        "lexical",
        "lexical",
    ]
    assert len({id(c["candidate_trace"]) for c in calls}) == 6
    for name in ("baseline", "candidate", "lexical"):
        report = comparison[name]
        assert report["diagnostics"]["snapshot"]["fixture_sha256"] == original_hash
        assert [r["candidate_analysis"]["case_index"] for r in report["results"]] == [
            0,
            1,
        ]
        for cap in (1, 2):
            expected = evaluation.compute_search_eval(
                cases,
                results_by_query={
                    "same": (
                        ["one:main.py", "one:other.py"]
                        if cap == 1
                        else ["one:main.py", "one:main.py", "one:other.py"]
                    )
                },
                k=3,
                provider="test",
                model="test",
                dimensions=2,
            )
            actual = report["candidate_analysis"]["caps"][f"cap_{cap}"]
            for metric, value in expected.summary.items():
                if "latency" not in metric:
                    assert actual[metric] == value
            assert actual["mrr_at_k"] == 0.5  # unlabelled query retained
        if name != "baseline":
            assert (
                report["results"][0]["candidate_analysis"]["lexical_ties"][
                    "largest_tie"
                ]
                == 3
            )


@pytest.mark.parametrize("mode", ["semantic", "lexical", "hybrid"])
def test_empty_candidate_analysis(tmp_path, monkeypatch, mode):
    fixture, _, _, calls = install_fixture(tmp_path, monkeypatch, [])
    report = evaluation.run_search_eval(
        "p", fixture, mode=mode, analyze_candidates=True
    )
    assert len(calls) == 1
    row = report.results[0].candidate_analysis
    assert row["candidate_pool"]["total_available"] == 0
    assert row["caps"]["cap_1"]["unfilled_slots"] == 5
    assert report.candidate_analysis["candidate_pool"]["expected_paths"] == 3
    assert report.candidate_analysis["caps"]["cap_1"]["recall_at_k"] == 0


@pytest.mark.parametrize("k", [0, 101, True])
def test_analysis_validates_k_before_fixture_snapshot_or_search(monkeypatch, k):
    load = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(evaluation, "load_eval_corpus", load)
    monkeypatch.setattr(evaluation, "_load_fixture_snapshot", load)
    monkeypatch.setattr(evaluation, "search_project_context", load)
    with pytest.raises(ValueError):
        evaluation.run_search_eval("p", "unused", k=k, analyze_candidates=True)
    with pytest.raises(ValueError):
        evaluation.compare_search_eval("p", "unused", k=k, analyze_candidates=True)
    load.assert_not_called()


def test_analysis_scope_failure_before_search_or_embedding(tmp_path, monkeypatch):
    fixture, _, load, calls = install_fixture(tmp_path, monkeypatch, [])
    load.side_effect = ValueError("Unknown repositories")
    monkeypatch.setattr(
        evaluation, "embedding_dimension", Mock(side_effect=AssertionError("embedding"))
    )
    with pytest.raises(ValueError, match="Unknown repositories"):
        evaluation.run_search_eval("p", fixture, analyze_candidates=True)
    assert not calls
