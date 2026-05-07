from __future__ import annotations

from gpc.search_eval import SearchEvalCase, compute_search_eval


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

    report = compute_search_eval(cases, results_by_query=results_by_query, k=2)

    assert report.summary["queries"] == 2
    assert report.summary["expected_paths"] == 3
    assert report.summary["hits"] == 2
    assert report.summary["recall_at_k"] == 2 / 3
    assert report.results[0].missing_expected_paths == ["SECURITY.md"]


def test_compute_search_eval_marks_query_with_no_results_as_missing() -> None:
    cases = [SearchEvalCase(query="missing", expected_paths=["README.md"])]

    report = compute_search_eval(cases, results_by_query={"missing": []}, k=5)

    assert report.summary["hits"] == 0
    assert report.results[0].hit_paths == []
    assert report.results[0].missing_expected_paths == ["README.md"]
