from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

import pytest

from gpc.graph import neo4j_driver
from gpc.graphify_freshness import measure_sync_freshness

TEST_REPO_SLUG = "freshness-worker"


@pytest.mark.integration
def test_measure_sync_freshness_reports_queryable_lag() -> None:
    project_slug = f"test-freshness-{uuid.uuid4().hex}"
    repo_root = _make_synthetic_repo(
        graph={
            "nodes": [
                {
                    "id": "fresh_entrypoint",
                    "label": "FreshWorkerEntrypoint",
                    "file_type": "code",
                    "source_file": "src/worker.py",
                },
                {
                    "id": "fresh_contract",
                    "label": "FreshContract",
                    "file_type": "code",
                    "source_file": "src/contracts.py",
                    "qualified_name": "freshness.FreshContract",
                },
            ],
            "links": [
                {
                    "source": "fresh_entrypoint",
                    "target": "fresh_contract",
                    "relation": "USES",
                }
            ],
        }
    )

    try:
        _cleanup_graphify_project(project_slug)
        result = measure_sync_freshness(
            repo_root,
            project_slug=project_slug,
            repo_slug=TEST_REPO_SLUG,
            clear_repo=True,
        )

        assert result.project_slug == project_slug
        assert result.repo_slug == TEST_REPO_SLUG
        assert (
            result.sync_started_at
            <= result.sync_finished_at
            <= result.first_queryable_at
        )
        assert result.sync_stats.nodes_written == 2
        assert result.sync_stats.relations_written == 1
        assert result.observable_node_count == 2
        assert isinstance(result.lag_seconds, float)
        assert 0.0 <= result.lag_seconds < 5.0
    finally:
        _cleanup_graphify_project(project_slug)
        shutil.rmtree(repo_root, ignore_errors=True)


def _make_synthetic_repo(*, graph: dict) -> Path:
    base_dir = Path(__file__).resolve().parent / "_runtime"
    repo_root = base_dir / f"graphify-freshness-{uuid.uuid4().hex}"
    graph_dir = repo_root / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=False)
    (graph_dir / "graph.json").write_text(
        json.dumps(graph),
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
