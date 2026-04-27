"""Smoke test for rule-based drift detection."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN
from gpc.drift import detect_drift, list_drift_signals
from gpc.registry import connect, ensure_project


TEST_PROJECT_SLUG = "gpc_smoke_drift"


def _cleanup() -> None:
    with connect() as conn:
        conn.execute("delete from gpc_projects where slug = %s", (TEST_PROJECT_SLUG,))
        conn.execute(
            "delete from gpc_self_metrics where project_slug = %s",
            (TEST_PROJECT_SLUG,),
        )
        conn.execute(
            "delete from gpc_drift_signals where project_slug = %s",
            (TEST_PROJECT_SLUG,),
        )


def _snapshot(
    *,
    project_id: str,
    inferred: int,
    extracted: int,
    weakly_connected: int,
    communities: int,
    god_nodes: list[str],
) -> str:
    with psycopg.connect(POSTGRES_DSN) as conn:
        row = conn.execute(
            """
            insert into gpc_self_metrics (
                project_id, project_slug, source,
                extracted_count, inferred_count, ambiguous_count,
                weakly_connected_nodes, community_count, god_nodes_top10
            )
            values (%s, %s, 'snapshot', %s, %s, 0, %s, %s, %s)
            returning id::text
            """,
            (
                project_id,
                TEST_PROJECT_SLUG,
                extracted,
                inferred,
                weakly_connected,
                communities,
                Jsonb([{"label": label} for label in god_nodes]),
            ),
        ).fetchone()
    return row[0]


def main() -> None:
    _cleanup()
    try:
        project = ensure_project(slug=TEST_PROJECT_SLUG, name="Drift Smoke")
        project_id = project["id"]

        snap_a = _snapshot(
            project_id=project_id,
            extracted=900,
            inferred=100,
            weakly_connected=20,
            communities=4,
            god_nodes=["alpha", "beta", "gamma"],
        )
        snap_b = _snapshot(
            project_id=project_id,
            extracted=700,
            inferred=300,
            weakly_connected=45,
            communities=9,
            god_nodes=["delta", "alpha", "beta"],
        )

        result = detect_drift(
            TEST_PROJECT_SLUG,
            from_id=snap_a,
            to_id=snap_b,
            persist=True,
        )
        assert result["found"] is True, result
        signal_types = {signal["signal_type"] for signal in result["signals"]}
        assert "confidence_inferred_shift" in signal_types, result
        assert "weakly_connected_spike" in signal_types, result
        assert "community_count_spike" in signal_types, result
        assert "top3_god_nodes_changed" in signal_types, result
        assert result["persisted"] == len(result["signals"]), result

        rows = list_drift_signals(project_slug=TEST_PROJECT_SLUG)
        assert len(rows) == result["persisted"], rows

    finally:
        _cleanup()

    print("drift_smoke_test=passed")


if __name__ == "__main__":
    main()
