from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

from gpc.graph import neo4j_driver
from gpc.graph_test_portfolio import (
    _is_under_test_roots,
    _module_reference,
    generate_test_portfolio_report,
)
from gpc.graphify_sync import sync_graphify_to_neo4j

TEST_PROJECT_SLUG = "test-portfolio-demo"
REPO_SLUG = "demo-repo"


def test_module_reference_converts_path_to_dotted_module() -> None:
    assert _module_reference("gpc/search.py") == "gpc.search"
    assert _module_reference("gpc/nested/module.py") == "gpc.nested.module"
    assert _module_reference(None) is None
    assert _module_reference("README.md") is None


def test_is_under_test_roots_matches_prefix_only() -> None:
    assert _is_under_test_roots("tests/smoke/foo_test.py", ("tests",)) is True
    assert _is_under_test_roots("tests_helpers/foo.py", ("tests",)) is False
    assert _is_under_test_roots("gpc/search.py", ("tests",)) is False


def test_generate_test_portfolio_report_flags_uncovered_external_call() -> None:
    repo_root = _make_synthetic_repo(
        nodes=[
            {
                "id": "god_caller",
                "label": "GodCaller",
                "file_type": "code",
                "source_file": "gpc/uncovered_module.py",
            },
            *_high_degree_fanout("god_caller", count=12),
            {
                "id": "requests_ext",
                "label": "requests",
                "file_type": "concept",
                "external": True,
                "type": "external",
            },
        ],
        links=[
            *[
                {"source": "god_caller", "target": f"peer_{i}", "relation": "calls"}
                for i in range(12)
            ],
            {"source": "god_caller", "target": "requests_ext", "relation": "imports"},
        ],
    )

    try:
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        sync_graphify_to_neo4j(
            repo_root,
            project_slug=TEST_PROJECT_SLUG,
            repo_slug=REPO_SLUG,
            clear_repo=True,
        )

        report = generate_test_portfolio_report(
            TEST_PROJECT_SLUG,
            test_roots=("tests",),
            repo_root=repo_root,
            min_god_node_degree=10,
        )

        assert report["found"] is True
        assert report["external_calls_checked"] >= 1
        gap_files = {gap["source_file"] for gap in report["gaps"]}
        assert "gpc/uncovered_module.py" in gap_files
    finally:
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        shutil.rmtree(repo_root, ignore_errors=True)


def test_generate_test_portfolio_report_treats_referenced_module_as_covered(tmp_path) -> None:
    repo_root = _make_synthetic_repo(
        nodes=[
            {
                "id": "god_caller",
                "label": "GodCaller",
                "file_type": "code",
                "source_file": "gpc/covered_module.py",
            },
            *_high_degree_fanout("god_caller", count=12),
            {
                "id": "requests_ext",
                "label": "requests",
                "file_type": "concept",
                "external": True,
                "type": "external",
            },
        ],
        links=[
            *[
                {"source": "god_caller", "target": f"peer_{i}", "relation": "calls"}
                for i in range(12)
            ],
            {"source": "god_caller", "target": "requests_ext", "relation": "imports"},
        ],
    )
    fake_test_root = tmp_path / "tests"
    fake_test_root.mkdir()
    (fake_test_root / "test_covered_module.py").write_text(
        "from gpc.covered_module import something  # exercises gpc.covered_module\n",
        encoding="utf-8",
    )

    try:
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        sync_graphify_to_neo4j(
            repo_root,
            project_slug=TEST_PROJECT_SLUG,
            repo_slug=REPO_SLUG,
            clear_repo=True,
        )

        report = generate_test_portfolio_report(
            TEST_PROJECT_SLUG,
            test_roots=("tests",),
            repo_root=tmp_path,
            min_god_node_degree=10,
        )

        assert report["found"] is True
        gap_files = {gap["source_file"] for gap in report["gaps"]}
        assert "gpc/covered_module.py" not in gap_files
        assert report["external_calls_covered"] >= 1
    finally:
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        shutil.rmtree(repo_root, ignore_errors=True)


def _high_degree_fanout(source_id: str, *, count: int) -> list[dict]:
    return [
        {
            "id": f"peer_{i}",
            "label": f"Peer{i}",
            "file_type": "code",
            "source_file": f"gpc/peer_{i}.py",
        }
        for i in range(count)
    ]


def _make_synthetic_repo(*, nodes: list[dict], links: list[dict]) -> Path:
    base_dir = Path(__file__).resolve().parent / "_runtime"
    repo_root = base_dir / f"test-portfolio-{uuid.uuid4().hex}"
    graph_dir = repo_root / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=False)
    (graph_dir / "graph.json").write_text(
        json.dumps({"nodes": nodes, "links": links}),
        encoding="utf-8",
    )
    return repo_root


def _cleanup_graphify_project(project_slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})
                OPTIONAL MATCH (p)-[:HAS_REPO]->(r:GraphifyRepo)
                OPTIONAL MATCH (r)-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                DETACH DELETE n, r, p
                """,
                slug=project_slug,
            )
