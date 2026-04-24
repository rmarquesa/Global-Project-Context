"""Smoke test for the self_metrics collector + graph_diff.

Seeds a throwaway project, records two snapshots with deliberately different
counts, then asserts ``graph_diff`` reports the right deltas across each
axis (numeric counts, god-node churn, confidence distribution shift).
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN
from gpc.graph_query import graph_diff
from gpc.registry import connect, ensure_project, register_repo
from gpc.self_metrics import collect_metrics, list_snapshots


TEST_PROJECT_SLUG = "gpc_smoke_metrics"


def _cleanup() -> None:
    with connect() as conn:
        conn.execute("delete from gpc_projects where slug = %s", (TEST_PROJECT_SLUG,))
        conn.execute(
            "delete from gpc_self_metrics where project_slug = %s",
            (TEST_PROJECT_SLUG,),
        )


def _insert_snapshot_with_counts(
    *,
    files: int,
    chunks: int,
    entities: int,
    extracted: int,
    inferred: int,
    ambiguous: int,
    god_nodes: list[dict[str, str]],
    project_id: str,
) -> str:
    with psycopg.connect(POSTGRES_DSN) as conn:
        row = conn.execute(
            """
            insert into gpc_self_metrics (
                project_id, project_slug, source,
                files_count, chunks_count, entities_count,
                extracted_count, inferred_count, ambiguous_count,
                god_nodes_top10
            )
            values (%s, %s, 'snapshot', %s, %s, %s, %s, %s, %s, %s)
            returning id::text
            """,
            (
                project_id,
                TEST_PROJECT_SLUG,
                files,
                chunks,
                entities,
                extracted,
                inferred,
                ambiguous,
                Jsonb(god_nodes),
            ),
        ).fetchone()
    return row[0]


def main() -> None:
    _cleanup()
    try:
        project = ensure_project(slug=TEST_PROJECT_SLUG, name="Self Metrics Smoke")
        project_id = project["id"]

        snap_a = _insert_snapshot_with_counts(
            files=100,
            chunks=200,
            entities=100,
            extracted=800,
            inferred=150,
            ambiguous=50,
            god_nodes=[{"label": "alpha"}, {"label": "beta"}, {"label": "gamma"}],
            project_id=project_id,
        )
        snap_b = _insert_snapshot_with_counts(
            files=105,
            chunks=215,
            entities=105,
            extracted=900,
            inferred=100,
            ambiguous=25,
            god_nodes=[{"label": "alpha"}, {"label": "beta"}, {"label": "delta"}],
            project_id=project_id,
        )

        diff = graph_diff(TEST_PROJECT_SLUG, from_id=snap_a, to_id=snap_b)
        assert diff["found"] is True, diff
        deltas = diff["numeric_deltas"]
        assert deltas["files_count"]["delta"] == 5, deltas
        assert deltas["chunks_count"]["delta"] == 15, deltas
        assert deltas["extracted_count"]["delta"] == 100, deltas
        assert deltas["inferred_count"]["delta"] == -50, deltas

        god_diff = diff["god_nodes_diff"]
        assert god_diff["entered"] == ["delta"], god_diff
        assert god_diff["exited"] == ["gamma"], god_diff
        assert set(god_diff["stable"]) == {"alpha", "beta"}, god_diff

        shift = diff["confidence_shift"]
        # Before: 800/1000 extracted = 80%. After: 900/1025 extracted ≈ 87.8%.
        # So the EXTRACTED share should rise by ~7.8 pp.
        assert shift["delta_pp"]["EXTRACTED"] > 5.0, shift
        assert shift["delta_pp"]["INFERRED"] < 0.0, shift
        assert shift["delta_pp"]["AMBIGUOUS"] < 0.0, shift

        # Window-based lookup: default 24h picks the latest snapshot but no
        # prior one older than 24h, so we expect found=False with reason.
        diff_recent = graph_diff(TEST_PROJECT_SLUG, window_hours=24)
        assert diff_recent["found"] is False, diff_recent
        assert diff_recent["reason"] == "no_prior_snapshot_in_window", diff_recent

        # Listing snapshots returns both in reverse chronological order.
        rows = list_snapshots(project_slug=TEST_PROJECT_SLUG, limit=10)
        assert len(rows) == 2, rows

        # Live collect writes a third row without touching the prior two.
        live = collect_metrics(project_slug=TEST_PROJECT_SLUG, source="snapshot")
        assert live.id, live
        rows2 = list_snapshots(project_slug=TEST_PROJECT_SLUG, limit=10)
        assert len(rows2) == 3, rows2

    finally:
        _cleanup()

    print("self_metrics_smoke_test=passed")


if __name__ == "__main__":
    main()
