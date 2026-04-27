from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN
from gpc.graph_query import graph_diff
from gpc.self_metrics import fetch_snapshot


@dataclass(frozen=True)
class DriftSignal:
    severity: str
    signal_type: str
    message: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_drift(
    project_slug: str,
    *,
    window_hours: int | None = 24,
    from_id: str | None = None,
    to_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    diff = graph_diff(
        project_slug,
        window_hours=window_hours,
        from_id=from_id,
        to_id=to_id,
    )
    if not diff.get("found"):
        return {
            "project_slug": project_slug,
            "found": False,
            "reason": diff.get("reason"),
            "signals": [],
            "persisted": 0,
            "diff": diff,
        }

    signals = _signals_from_diff(diff)
    persisted = _persist_signals(project_slug, diff, signals) if persist else 0
    return {
        "project_slug": project_slug,
        "found": True,
        "signals": [signal.as_dict() for signal in signals],
        "persisted": persisted,
        "diff": diff,
    }


def list_drift_signals(
    *,
    project_slug: str | None = None,
    unresolved_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    where = []
    params: list[Any] = []
    if project_slug:
        where.append("project_slug = %s")
        params.append(project_slug)
    if unresolved_only:
        where.append("resolved_at is null")
    clause = "where " + " and ".join(where) if where else ""
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            f"""
            select id::text as id, created_at, project_slug, severity,
                   signal_type, message,
                   from_snapshot_id::text as from_snapshot_id,
                   to_snapshot_id::text as to_snapshot_id,
                   evidence, resolved_at
            from gpc_drift_signals
            {clause}
            order by created_at desc
            limit %s
            """,
            (*params, limit),
        ).fetchall()


def _signals_from_diff(diff: dict[str, Any]) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    deltas = diff.get("numeric_deltas") or {}
    shift = (diff.get("confidence_shift") or {}).get("delta_pp") or {}
    god = diff.get("god_nodes_diff") or {}

    inferred_delta = _num(shift.get("INFERRED"))
    if inferred_delta is not None and inferred_delta > 10:
        signals.append(
            DriftSignal(
                severity="warning",
                signal_type="confidence_inferred_shift",
                message=f"INFERRED graph share increased by {inferred_delta:.2f} percentage points.",
                evidence={"delta_pp": inferred_delta, "confidence_shift": diff.get("confidence_shift")},
            )
        )

    ambiguous_delta = _num(shift.get("AMBIGUOUS"))
    if ambiguous_delta is not None and ambiguous_delta > 5:
        signals.append(
            DriftSignal(
                severity="warning",
                signal_type="confidence_ambiguous_shift",
                message=f"AMBIGUOUS graph share increased by {ambiguous_delta:.2f} percentage points.",
                evidence={"delta_pp": ambiguous_delta, "confidence_shift": diff.get("confidence_shift")},
            )
        )

    weak = deltas.get("weakly_connected_nodes") or {}
    weak_before = _num(weak.get("before"))
    weak_after = _num(weak.get("after"))
    weak_delta = _num(weak.get("delta"))
    if (
        weak_before is not None
        and weak_after is not None
        and weak_delta is not None
        and weak_delta >= 10
        and weak_after > max(weak_before * 1.5, weak_before + 10)
    ):
        signals.append(
            DriftSignal(
                severity="warning",
                signal_type="weakly_connected_spike",
                message=f"Weakly connected nodes rose from {int(weak_before)} to {int(weak_after)}.",
                evidence={"before": weak_before, "after": weak_after, "delta": weak_delta},
            )
        )

    communities = deltas.get("community_count") or {}
    community_before = _num(communities.get("before"))
    community_after = _num(communities.get("after"))
    community_delta = _num(communities.get("delta"))
    if (
        community_before is not None
        and community_after is not None
        and community_delta is not None
        and community_delta >= 3
        and community_after > max(community_before * 1.5, community_before + 3)
    ):
        signals.append(
            DriftSignal(
                severity="info",
                signal_type="community_count_spike",
                message=f"Graph community count rose from {int(community_before)} to {int(community_after)}.",
                evidence={"before": community_before, "after": community_after, "delta": community_delta},
            )
        )

    entered = god.get("entered") or []
    exited = god.get("exited") or []
    if entered or exited:
        signals.append(
            DriftSignal(
                severity="info",
                signal_type="god_nodes_changed",
                message="Top graph hubs changed.",
                evidence={"entered": entered, "exited": exited, "stable": god.get("stable") or []},
            )
        )

    top3 = _top3_shift(diff)
    if top3:
        signals.append(top3)

    return signals


def _top3_shift(diff: dict[str, Any]) -> DriftSignal | None:
    frm_id = (diff.get("from") or {}).get("id")
    to_id = (diff.get("to") or {}).get("id")
    if not frm_id or not to_id:
        return None
    frm = fetch_snapshot(frm_id)
    to = fetch_snapshot(to_id)
    if not frm or not to:
        return None
    before = [g.get("label") for g in (frm.get("god_nodes_top10") or [])[:3] if g.get("label")]
    after = [g.get("label") for g in (to.get("god_nodes_top10") or [])[:3] if g.get("label")]
    if before and after and before != after:
        return DriftSignal(
            severity="warning",
            signal_type="top3_god_nodes_changed",
            message="Top-3 graph hubs changed.",
            evidence={"before": before, "after": after},
        )
    return None


def _persist_signals(
    project_slug: str,
    diff: dict[str, Any],
    signals: list[DriftSignal],
) -> int:
    if not signals:
        return 0
    project_id = _project_id(project_slug)
    from_id = (diff.get("from") or {}).get("id")
    to_id = (diff.get("to") or {}).get("id")
    with psycopg.connect(POSTGRES_DSN) as conn:
        for signal in signals:
            conn.execute(
                """
                insert into gpc_drift_signals (
                    project_id, project_slug, severity, signal_type, message,
                    from_snapshot_id, to_snapshot_id, evidence
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    project_slug,
                    signal.severity,
                    signal.signal_type,
                    signal.message,
                    from_id,
                    to_id,
                    Jsonb(signal.evidence),
                ),
            )
    return len(signals)


def _project_id(project_slug: str) -> Any:
    with psycopg.connect(POSTGRES_DSN) as conn:
        row = conn.execute(
            "select id from gpc_projects where slug = %s",
            (project_slug,),
        ).fetchone()
    return row[0] if row else None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
