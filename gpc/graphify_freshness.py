from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time

from gpc.graph import neo4j_driver
from gpc.graphify_sync import GraphifySyncStats, sync_graphify_to_neo4j

_QUERY_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class FreshnessResult:
    project_slug: str
    repo_slug: str
    sync_started_at: datetime
    sync_finished_at: datetime
    first_queryable_at: datetime
    lag_seconds: float
    observable_node_count: int
    sync_stats: GraphifySyncStats


def measure_sync_freshness(
    repo_root: str | Path,
    *,
    project_slug: str,
    repo_slug: str,
    clear_repo: bool = True,
) -> FreshnessResult:
    """Measure how long a Graphify sync takes to become queryable in Neo4j."""

    sync_started_at = datetime.now(timezone.utc)
    sync_stats = sync_graphify_to_neo4j(
        repo_root,
        project_slug=project_slug,
        repo_slug=repo_slug,
        clear_repo=clear_repo,
    )
    sync_finished_at = datetime.now(timezone.utc)

    observable_node_count, first_queryable_at = _wait_for_repo_visibility(
        project_slug=project_slug,
        repo_slug=repo_slug,
        expected_min_nodes=1 if sync_stats.nodes_written else 0,
    )
    lag_seconds = max(0.0, (first_queryable_at - sync_finished_at).total_seconds())

    return FreshnessResult(
        project_slug=project_slug,
        repo_slug=repo_slug,
        sync_started_at=sync_started_at,
        sync_finished_at=sync_finished_at,
        first_queryable_at=first_queryable_at,
        lag_seconds=lag_seconds,
        observable_node_count=observable_node_count,
        sync_stats=sync_stats,
    )


def _wait_for_repo_visibility(
    *,
    project_slug: str,
    repo_slug: str,
    expected_min_nodes: int,
    timeout_seconds: float = _QUERY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> tuple[int, datetime]:
    deadline = time.monotonic() + timeout_seconds
    last_count = 0

    while True:
        last_count = _graphify_repo_node_count(
            project_slug=project_slug, repo_slug=repo_slug
        )
        observed_at = datetime.now(timezone.utc)
        if last_count >= expected_min_nodes:
            return last_count, observed_at
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for synced Graphify data to become queryable "
                f"for {project_slug}:{repo_slug}; last observed node_count={last_count}."
            )
        time.sleep(poll_interval_seconds)


def _graphify_repo_node_count(*, project_slug: str, repo_slug: str) -> int:
    with neo4j_driver() as driver:
        with driver.session() as session:
            record = session.run(
                """
                MATCH (:GraphifyProject {slug: $project_slug})-[:HAS_REPO]->(repo:GraphifyRepo {
                    id: $repo_id,
                    slug: $repo_slug,
                    project_slug: $project_slug
                })-[:HAS_GRAPH_NODE]->(node:GraphifyNode {
                    repo_id: $repo_id,
                    repo_slug: $repo_slug,
                    project_slug: $project_slug
                })
                RETURN count(node) AS node_count
                """,
                project_slug=project_slug,
                repo_slug=repo_slug,
                repo_id=f"{project_slug}:{repo_slug}",
            ).single()
    return int(record["node_count"]) if record else 0
