from __future__ import annotations

import builtins
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

import gpc.search_eval as evaluation
from gpc.search_eval import SearchEvalCase, compute_search_eval


@pytest.fixture(autouse=True)
def embedding_metadata(monkeypatch):
    monkeypatch.setattr(evaluation, "active_embedding_model", lambda: "test-model")
    monkeypatch.setattr(evaluation, "embedding_dimension", lambda: 16)


def fixture_file(tmp_path, text):
    path = tmp_path / "eval.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def found(repo, path):
    return SimpleNamespace(repo_slug=repo, relative_path=path)


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected backend or embedding call")


def test_compute_search_eval_counts_expected_path_hits() -> None:
    cases = [
        SearchEvalCase(
            query="read only mcp",
            expected_paths=["docs/architecture.md", "SECURITY.md"],
        ),
        SearchEvalCase(query="registry", expected_paths=["gpc/registry.py"]),
    ]
    results_by_query = {
        "read only mcp": ["README.md", "docs/architecture.md"],
        "registry": ["gpc/cli.py", "gpc/registry.py"],
    }

    # Pass dimensions explicitly so the report doesn't probe Ollama for the
    # embedding dimension — this test only exercises the counting logic.
    report = compute_search_eval(
        cases, results_by_query=results_by_query, k=2, dimensions=768
    )

    assert report.summary["queries"] == 2
    assert report.summary["expected_paths"] == 3
    assert report.summary["hits"] == 2
    assert report.summary["recall_at_k"] == 2 / 3
    assert report.results[0].missing_expected_paths == ["SECURITY.md"]


def test_compute_search_eval_marks_query_with_no_results_as_missing() -> None:
    cases = [SearchEvalCase(query="missing", expected_paths=["README.md"])]

    report = compute_search_eval(
        cases, results_by_query={"missing": []}, k=5, dimensions=768
    )

    assert report.summary["hits"] == 0
    assert report.results[0].hit_paths == []
    assert report.results[0].missing_expected_paths == ["README.md"]


def test_rank_metrics_and_duplicate_results() -> None:
    report = compute_search_eval(
        [SearchEvalCase("q", ["b", "a", "b"])],
        results_by_query={"q": ["x", "b", "b", "a"]},
        k=4,
        dimensions=0,
    )
    assert report.summary["mrr_at_k"] == 0.5
    assert report.summary["ndcg_at_k"] == (
        1 / 1.584962500721156 + 1 / 2.321928094887362
    ) / (1 + 1 / 1.584962500721156)
    assert report.results[0].expected_paths == ["b", "a"]


def test_zero_dimensions_do_not_probe_embedding_and_k_must_be_positive(
    monkeypatch,
) -> None:
    import gpc.search_eval as evaluation

    monkeypatch.setattr(
        evaluation,
        "embedding_dimension",
        lambda: (_ for _ in ()).throw(AssertionError()),
    )
    report = compute_search_eval(
        [], results_by_query={}, k=1, dimensions=0, mode="lexical"
    )
    assert report.dimensions == 0
    assert report.summary["ndcg_at_k"] == 0
    import pytest

    with pytest.raises(ValueError):
        compute_search_eval([], results_by_query={}, k=0)


def test_runner_legacy_unqualified_labels_and_qualified_reporting(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path,
        'queries:\n  - query: "legacy"\n    expected_paths:\n      - "src/config.py"\n',
    )
    monkeypatch.setattr(
        evaluation,
        "search_project_context",
        lambda *a, **kw: (None, [found("repo-a", "src/config.py")]),
    )
    report = evaluation.run_search_eval("p", fixture)
    assert report.summary["recall_at_k"] == 1
    assert report.summary["ndcg_at_k"] == 1
    assert report.results[0].result_paths == ["repo-a:src/config.py"]
    assert report.results[0].hit_paths == ["src/config.py"]
    assert report.results[0].missing_expected_paths == []
    # Filtering is a retrieval concern, not a rewrite of fixture labels.
    scoped = compute_search_eval(
        [SearchEvalCase("q", ["x"], repo="r")],
        results_by_query={"q": ["x"]},
        k=1,
        dimensions=0,
    )
    assert scoped.summary["recall_at_k"] == 1


@pytest.mark.parametrize("repo,hit", [("wrong", 0), ("right", 1)])
def test_qualified_labels_do_not_match_another_repo(repo, hit):
    report = compute_search_eval(
        [SearchEvalCase("q", ["right:same.py"])],
        results_by_query={"q": [f"{repo}:same.py"]},
        k=1,
        dimensions=0,
    )
    assert report.summary["hits"] == hit


def test_mixed_labels_duplicates_and_known_binary_ndcg():
    report = compute_search_eval(
        [SearchEvalCase("q", ["a.py", "r:a.py", "r:b.py", "r:b.py"])],
        results_by_query={
            "q": ["r:irrelevant", "r:a.py", "r:a.py", "r:b.py", "other:a.py"]
        },
        k=5,
        provider="p",
        model="m",
        dimensions=0,
    )
    # Aliases at rank 2 produce one binary gain, duplicate chunks keep positions.
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert report.summary["ndcg_at_k"] == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(5)) / ideal
    )
    assert report.summary["mrr_at_k"] == 0.5
    assert report.summary["hits"] == 3
    assert 0 <= report.summary["ndcg_at_k"] <= 1
    assert report.results[0].result_paths[2] == "r:a.py"


def test_repeated_queries_preserve_repo_task_results_and_metadata(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path,
        """queries:
  - query: "same"
    expected_paths: ["one:x.py"]
    repo: "one"
    task: "debug"
  - query: "same"
    expected_paths: ["two:y.py"]
    repo: ["two"]
    task: "review"
""",
    )
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs))
        return None, (
            [found("one", "x.py")]
            if kwargs["repo"] == "one"
            else [found("two", "y.py")]
        )

    monkeypatch.setattr(evaluation, "search_project_context", search)
    report = evaluation.run_search_eval(
        "project", fixture, k=3, mode="hybrid", task="implement"
    )
    assert [r.result_paths for r in report.results] == [["one:x.py"], ["two:y.py"]]
    assert [(r.repo, r.task) for r in report.results] == [
        ("one", "debug"),
        (["two"], "review"),
    ]
    assert [(kw["repo"], kw["task"], kw["limit"]) for _, kw in calls] == [
        ("one", "debug", 3),
        (["two"], "review", 3),
    ]
    assert report.summary["hits"] == 2
    assert report.to_dict()["mode"] == "hybrid"
    assert report.to_dict()["task"] == "implement"


def test_lexical_runner_never_probes_embedding_metadata(tmp_path, monkeypatch):
    fixture = fixture_file(
        tmp_path, 'queries:\n  - query: "q"\n    expected_paths: ["x"]\n'
    )
    monkeypatch.setattr(evaluation, "active_embedding_model", forbidden)
    monkeypatch.setattr(evaluation, "embedding_dimension", forbidden)
    calls = []

    def search(*args, **kwargs):
        calls.append(kwargs)
        return None, [found("r", "x")]

    monkeypatch.setattr(evaluation, "search_project_context", search)
    report = evaluation.run_search_eval("p", fixture, mode="lexical", task="test")
    assert len(calls) == 1 and calls[0]["mode"] == "lexical"
    assert report.to_dict()["embedding"] == {
        "provider": "not_applicable",
        "model": "not_applicable",
        "dimensions": 0,
    }
    assert report.summary["mean_latency_ms"] >= 0
    assert "not performance claims" in report.to_dict()["latency_note"]


def test_compare_same_cases_distinct_modes_signed_deltas_json_safe(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path,
        """queries:
  - query: "q1"
    expected_paths: ["x"]
  - query: "q2"
    expected_paths: ["y"]
    repo: "r"
    task: "debug"
""",
    )
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs.copy()))
        path = "x" if query == "q1" else "y"
        return None, [found("r", path if kwargs["mode"] == "hybrid" else "miss")]

    monkeypatch.setattr(evaluation, "search_project_context", search)
    ticks = iter([0, 0.004, 1, 1.004, 2, 2.002, 3, 3.002])
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: next(ticks))
    comparison = evaluation.compare_search_eval("p", fixture, k=2, task="review")
    assert [kw["mode"] for _, kw in calls] == [
        "semantic",
        "semantic",
        "hybrid",
        "hybrid",
    ]
    assert [
        (q, {k: v for k, v in kw.items() if k != "mode"}) for q, kw in calls[:2]
    ] == [(q, {k: v for k, v in kw.items() if k != "mode"}) for q, kw in calls[2:]]
    assert [r["query"] for r in comparison["baseline"]["results"]] == ["q1", "q2"]
    assert comparison["delta"]["recall_at_k"] == 1
    assert comparison["delta"]["mean_latency_ms"] == -2
    assert comparison["delta"]["p95_latency_ms"] == -2
    assert comparison["baseline"]["k"] == comparison["candidate"]["k"] == 2
    for metric, delta in comparison["delta"].items():
        assert (
            delta
            == comparison["candidate"]["summary"][metric]
            - comparison["baseline"]["summary"][metric]
        )
    json.dumps(comparison, allow_nan=False)


@pytest.mark.parametrize(
    "options", [{"k": 0}, {"k": -2}, {"mode": "unknown"}, {"task": "unknown"}]
)
def test_invalid_options_fail_before_calls_even_empty_fixture(
    tmp_path, monkeypatch, options
):
    fixture = fixture_file(tmp_path, "queries: []\n")
    for name in (
        "search_project_context",
        "active_embedding_model",
        "embedding_dimension",
    ):
        monkeypatch.setattr(evaluation, name, forbidden)
    with pytest.raises(ValueError):
        evaluation.run_search_eval("p", fixture, **options)
    with pytest.raises(ValueError):
        compute_search_eval([], results_by_query={}, **({"k": 1} | options))


def test_invalid_case_override_fails_before_any_search(tmp_path, monkeypatch):
    fixture = fixture_file(
        tmp_path,
        'queries:\n  - query: "valid"\n  - query: "invalid"\n    task: "unknown"\n',
    )
    monkeypatch.setattr(evaluation, "search_project_context", forbidden)
    with pytest.raises(ValueError, match="task"):
        evaluation.run_search_eval("p", fixture)


@pytest.mark.parametrize("options", [{"k": 0}, {"k": -1}, {"task": "unknown"}])
def test_compare_validates_before_loading_or_backend(monkeypatch, options):
    monkeypatch.setattr(evaluation, "load_search_eval_fixture", forbidden)
    monkeypatch.setattr(evaluation, "search_project_context", forbidden)
    with pytest.raises(ValueError):
        evaluation.compare_search_eval("p", "unused", **options)


def test_empty_lexical_fixture_is_safe_without_backends(tmp_path, monkeypatch):
    fixture = fixture_file(tmp_path, "queries: []\n")
    for name in (
        "search_project_context",
        "active_embedding_model",
        "embedding_dimension",
    ):
        monkeypatch.setattr(evaluation, name, forbidden)
    report = evaluation.run_search_eval("p", fixture, mode="lexical")
    assert report.results == []
    assert all(value == 0 for value in report.summary.values())


def test_empty_labels_cases_and_nearest_rank_latency():
    empty = compute_search_eval(
        [], results_by_query={}, k=1, provider="p", model="m", dimensions=0
    )
    assert all(value == 0 for value in empty.summary.values())
    report = compute_search_eval(
        [SearchEvalCase("q", [])],
        results_by_query={"q": ["x"]},
        k=1,
        provider="p",
        model="m",
        dimensions=0,
        latency_by_query={"q": 12},
    )
    assert report.summary["query_hit_rate"] == 0
    assert report.summary["mrr_at_k"] == report.summary["ndcg_at_k"] == 0
    assert report.summary["mean_latency_ms"] == report.summary["p95_latency_ms"] == 12
    rows = [SearchEvalCase(str(i), []) for i in range(20)]
    timed = compute_search_eval(
        rows,
        results_by_query={},
        k=1,
        provider="p",
        model="m",
        dimensions=0,
        latency_by_query={str(i): i + 1 for i in range(20)},
    )
    assert timed.summary["mean_latency_ms"] == 10.5
    assert timed.summary["p95_latency_ms"] == 19


def test_explicit_metadata_including_zero_dimensions_is_deterministic(monkeypatch):
    monkeypatch.setattr(evaluation, "active_embedding_model", forbidden)
    monkeypatch.setattr(evaluation, "embedding_dimension", forbidden)
    kwargs = dict(
        results_by_query={"q": ["x"]}, k=1, provider="p", model="m", dimensions=0
    )
    first = compute_search_eval([SearchEvalCase("q", ["x"])], **kwargs)
    assert (
        first.to_dict()
        == compute_search_eval([SearchEvalCase("q", ["x"])], **kwargs).to_dict()
    )
    assert first.dimensions == 0


FIXTURE_TEXT = """queries:
  - query: "legacy"
    expected_paths:
      - "src/a.py"
    task: "debug"
    repo: "one"
  - query: "inline"
    repo: [one, "two"]
    expected_paths: []
  - query: "block"
    repo:
      - "one"
      - "two"
    expected_paths:
      - "two:src/b.py"
    task: "test"
"""


def test_yaml_and_fallback_fixture_equivalence(tmp_path):
    yaml = pytest.importorskip("yaml")
    fixture = fixture_file(tmp_path, FIXTURE_TEXT)
    assert evaluation.load_search_eval_fixture(
        fixture
    ) == evaluation._parse_simple_fixture(FIXTURE_TEXT)
    assert evaluation._parse_simple_fixture(
        FIXTURE_TEXT
    ) == evaluation._cases_from_payload(yaml.safe_load(FIXTURE_TEXT))
    assert evaluation.load_search_eval_fixture(fixture)[1].repo == ["one", "two"]


def test_actual_packaged_fixture_without_yaml(monkeypatch):
    original = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("optional dependency unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    path = Path(__file__).parent / "fixtures" / "hybrid_search_eval.yaml"
    cases = evaluation.load_search_eval_fixture(path)
    assert len(cases) == 4
    assert cases[-1].expected_paths == ["repo-a:src/config.py"]
    assert cases[-1].repo == "repo-a"
    fixture_cases = evaluation._parse_simple_fixture(FIXTURE_TEXT)
    assert [c.repo for c in fixture_cases] == ["one", ["one", "two"], ["one", "two"]]
    assert fixture_cases[0].expected_paths == ["src/a.py"]
    assert fixture_cases[0].task == "debug"
    assert fixture_cases[1].expected_paths == []


@pytest.mark.parametrize(
    "text",
    [
        "other: []",
        "queries: invalid",
        "queries:\n  - expected_paths: []",
        'queries:\n  - query: "q"\n    expected_paths: "not a list"',
        'queries:\n  - query: "q"\n    repo: [one, [two]]',
        'queries:\n  - query: "q"\n    task: "unknown"',
        'queries:\n  - query: "q"\n    extra: []',
        'queries:\n  - query: "q"\n    expected_paths: ["unterminated]',
    ],
)
def test_malformed_fixtures_fail_clearly_with_both_parsers(tmp_path, text):
    with pytest.raises(ValueError):
        evaluation._parse_simple_fixture(text)
    path = fixture_file(tmp_path, text)
    with pytest.raises(ValueError):
        evaluation.load_search_eval_fixture(path)


LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "search_eval_gpc.yml"


def force_fallback(monkeypatch):
    original = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("optional dependency unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)


def test_shipped_legacy_yml_has_four_identical_cases_with_both_parsers(monkeypatch):
    pytest.importorskip("yaml")
    normal = evaluation.load_search_eval_fixture(LEGACY_FIXTURE)
    force_fallback(monkeypatch)
    fallback = evaluation.load_search_eval_fixture(LEGACY_FIXTURE)
    assert normal == fallback
    assert len(fallback) == 4
    assert all(case.expected_paths for case in fallback)
    assert fallback[0].query == "how does MCP stay read-only?"
    assert fallback[-1].expected_paths[-1] == "gpc/drift.py"


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "runtime_embedding",
    [
        {"provider": "ollama", "model": "nomic-embed-text:latest", "dimensions": 768},
        {"provider": "runtime-provider", "model": "runtime-model", "dimensions": 32},
    ],
)
def test_cli_shipped_fixture_json_uses_runtime_embedding_metadata(
    monkeypatch, capsys, fallback, runtime_embedding
):
    from gpc.cli import cmd_eval_search

    if fallback:
        force_fallback(monkeypatch)
    monkeypatch.setattr(evaluation, "EMBEDDING_PROVIDER", runtime_embedding["provider"])
    monkeypatch.setattr(
        evaluation, "active_embedding_model", lambda: runtime_embedding["model"]
    )
    monkeypatch.setattr(
        evaluation, "embedding_dimension", lambda: runtime_embedding["dimensions"]
    )
    cases = evaluation.load_search_eval_fixture(LEGACY_FIXTURE)
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs))
        case = next(case for case in cases if case.query == query)
        return None, [found("gpc", path) for path in case.expected_paths]

    monkeypatch.setattr(evaluation, "search_project_context", search)
    args = SimpleNamespace(
        project="gpc",
        fixture=LEGACY_FIXTURE,
        k=5,
        mode="semantic",
        task="general",
        compare=False,
        json=True,
    )
    assert cmd_eval_search(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["queries"] == 4
    assert payload["summary"]["recall_at_k"] == 1
    assert payload["embedding"] == runtime_embedding
    assert [query for query, _ in calls] == [case.query for case in cases]
    assert all(
        kwargs["project"] == "gpc" and kwargs["limit"] == 5 for _, kwargs in calls
    )


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "metadata",
    [
        "embedding: not-a-mapping",
        "embedding: []",
        "embedding: null",
        "embedding: {}\n  provider: ollama",
        "embedding:\n  unknown: value",
        "embedding:\n  provider: []",
        "embedding:\n  model: null",
        "embedding:\n  dimensions: -1",
        "embedding:\n  dimensions: 1.5",
        "embedding:\n  dimensions: true",
        'embedding:\n  dimensions: "768"',
    ],
)
def test_malformed_embedding_metadata_fails_clearly(
    tmp_path, monkeypatch, fallback, metadata
):
    if fallback:
        force_fallback(monkeypatch)
    path = fixture_file(tmp_path, metadata + "\nqueries: []\n")
    with pytest.raises(ValueError):
        evaluation.load_search_eval_fixture(path)


@pytest.mark.parametrize(
    "metadata",
    [
        "embedding: {}",
        "embedding:\n  dimensions: 0",
        "embedding: {provider: custom, model: custom:latest, dimensions: 768}",
        "embedding:\n  provider: custom\n  model: 'a, b'\n  dimensions: 768",
    ],
)
def test_optional_embedding_fields_and_inline_mapping_parser_parity(metadata):
    yaml = pytest.importorskip("yaml")
    text = metadata + '\nqueries:\n  - query: "q"\n    expected_paths: []\n'
    assert (
        evaluation._parse_simple_fixture(text)
        == evaluation._cases_from_payload(yaml.safe_load(text))
        == [SearchEvalCase("q", [])]
    )


def diagnostic_snapshot():
    from gpc.eval_corpus import EvalCorpusSnapshot

    return EvalCorpusSnapshot(
        "p",
        "project",
        (("1", "one"), ("2", "two")),
        (
            ("one", "indexed.py", "hash", True),
            ("one", "empty.py", "hash2", False),
            ("two", "second.py", "hash3", True),
        ),
    )


def test_diagnostics_distinguish_rank_miss_absence_without_changing_metrics(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path,
        """queries:
  - query: "q"
    expected_paths: ["indexed.py", "absent.py", "empty.py", "indexed.py"]
""",
    )
    monkeypatch.setattr(
        evaluation, "load_eval_corpus", lambda *args: diagnostic_snapshot()
    )
    monkeypatch.setattr(
        evaluation, "search_project_context", lambda *a, **kw: (None, [])
    )
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: 0)
    normal = evaluation.run_search_eval("p", fixture)
    diagnostic = evaluation.run_search_eval("p", fixture, diagnose=True)
    assert normal.summary == diagnostic.summary
    assert normal.summary["expected_paths"] == 3
    assert "diagnostics" not in normal.to_dict()
    assert "diagnostics" not in normal.results[0].to_dict()
    row = diagnostic.to_dict()["results"][0]
    assert row["missing_expected_paths"] == ["indexed.py", "absent.py", "empty.py"]
    assert row["diagnostics"]["indexed_but_not_retrieved_paths"] == ["indexed.py"]
    assert row["diagnostics"]["not_indexed_expected_paths"] == ["absent.py", "empty.py"]
    assert diagnostic.diagnostics["coverage"]["file_without_chunks"] == 1


def test_compare_all_reuses_fixture_bytes_snapshot_and_scoped_case_indices(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path,
        """queries:
  - query: "same"
    expected_paths: ["indexed.py"]
    repo: "one"
  - query: "same"
    expected_paths: ["second.py"]
    repo: "two"
""",
    )
    import hashlib

    fixture_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    reads, snapshots, calls = [], [], []
    original = evaluation._load_fixture_snapshot

    def load(path):
        reads.append(path)
        return original(path)

    def snapshot(project, cases):
        snapshots.append(cases)
        # Changing the file after parsing must not change cases or its fingerprint.
        fixture.write_text("queries: []\n")
        return diagnostic_snapshot()

    def search(query, **kwargs):
        calls.append((query, kwargs))
        hit = (kwargs["mode"] == "hybrid" and kwargs["repo"] == "one") or (
            kwargs["mode"] == "lexical" and kwargs["repo"] == "two"
        )
        path = "indexed.py" if kwargs["repo"] == "one" else "second.py"
        return None, [found(kwargs["repo"], path)] if hit else []

    monkeypatch.setattr(evaluation, "_load_fixture_snapshot", load)
    monkeypatch.setattr(evaluation, "load_eval_corpus", snapshot)
    monkeypatch.setattr(evaluation, "search_project_context", search)
    result = evaluation.compare_search_eval(
        "p", fixture, diagnose=True, include_lexical=True
    )
    assert len(reads) == len(snapshots) == 1
    assert [kw["mode"] for _, kw in calls] == [
        "semantic",
        "semantic",
        "hybrid",
        "hybrid",
        "lexical",
        "lexical",
    ]
    assert (
        result["delta"]["recall_at_k"] == result["lexical_delta"]["recall_at_k"] == 0.5
    )
    for name in ("baseline", "candidate", "lexical"):
        assert result[name]["diagnostics"]["snapshot"]["fixture_sha256"] == fixture_hash
        assert [r["diagnostics"]["case_index"] for r in result[name]["results"]] == [
            0,
            1,
        ]
    assert result["candidate"]["results"][1]["diagnostics"][
        "indexed_but_not_retrieved_paths"
    ] == ["second.py"]
    assert result["lexical"]["results"][0]["diagnostics"][
        "indexed_but_not_retrieved_paths"
    ] == ["indexed.py"]
    assert "not a benchmark" in result["comparison_note"]
    json.dumps(result, allow_nan=False)


def test_lexical_diagnostics_never_probes_ollama_and_default_never_loads_metadata(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path, 'queries:\n  - query: "q"\n    expected_paths: ["indexed.py"]\n'
    )
    monkeypatch.setattr(
        evaluation, "search_project_context", lambda *a, **kw: (None, [])
    )
    monkeypatch.setattr(evaluation, "active_embedding_model", forbidden)
    monkeypatch.setattr(evaluation, "embedding_dimension", forbidden)
    monkeypatch.setattr(evaluation, "load_eval_corpus", forbidden)
    normal = evaluation.run_search_eval("p", fixture, mode="lexical")
    assert "diagnostics" not in normal.to_dict()
    monkeypatch.setattr(
        evaluation, "load_eval_corpus", lambda *args: diagnostic_snapshot()
    )
    report = evaluation.run_search_eval("p", fixture, mode="lexical", diagnose=True)
    assert report.provider == "not_applicable" and report.dimensions == 0
    assert report.diagnostics["coverage"]["indexed"] == 1


@pytest.mark.parametrize("options", [{"k": 0}, {"k": 101}, {"task": "unknown"}])
def test_diagnostic_invalid_options_fail_before_metadata_or_embedding(
    monkeypatch, options
):
    for name in (
        "_load_fixture_snapshot",
        "load_eval_corpus",
        "search_project_context",
        "active_embedding_model",
        "embedding_dimension",
    ):
        monkeypatch.setattr(evaluation, name, forbidden)
    with pytest.raises(ValueError):
        evaluation.run_search_eval("p", "unused", diagnose=True, **options)
    with pytest.raises(ValueError):
        evaluation.compare_search_eval(
            "p", "unused", diagnose=True, include_lexical=True, **options
        )


def test_diagnostic_metadata_failure_is_not_silently_a_rank_miss(tmp_path, monkeypatch):
    fixture = fixture_file(
        tmp_path, 'queries:\n  - query: "q"\n    expected_paths: ["indexed.py"]\n'
    )

    def fail(*args):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(evaluation, "load_eval_corpus", fail)
    monkeypatch.setattr(evaluation, "search_project_context", forbidden)
    monkeypatch.setattr(evaluation, "embedding_dimension", forbidden)
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        evaluation.run_search_eval("p", fixture, diagnose=True)
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        evaluation.compare_search_eval(
            "p", fixture, diagnose=True, include_lexical=True
        )


def test_fixture_fingerprint_tracks_exact_parsed_bytes(tmp_path):
    fixture = fixture_file(tmp_path, "queries: []\n")
    first_cases, first_hash = evaluation._load_fixture_snapshot(fixture)
    assert evaluation._load_fixture_snapshot(fixture) == (first_cases, first_hash)
    fixture.write_bytes(b"queries: []\r\n")
    cases, digest = evaluation._load_fixture_snapshot(fixture)
    assert cases == first_cases and digest != first_hash


def test_default_comparison_retains_old_keys_and_never_loads_metadata(
    tmp_path, monkeypatch
):
    fixture = fixture_file(
        tmp_path, 'queries:\n  - query: "q"\n    expected_paths: ["indexed.py"]\n'
    )
    monkeypatch.setattr(evaluation, "load_eval_corpus", forbidden)
    monkeypatch.setattr(
        evaluation, "search_project_context", lambda *a, **kw: (None, [])
    )
    comparison = evaluation.compare_search_eval("p", fixture)
    assert set(comparison) == {"baseline", "candidate", "delta"}
    for name in ("baseline", "candidate"):
        assert "diagnostics" not in comparison[name]
        assert "diagnostics" not in comparison[name]["results"][0]
