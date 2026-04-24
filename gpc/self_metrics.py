"""Longitudinal metrics collector for the GPC graph.

Each call to :func:`collect_metrics` writes one row to ``gpc_self_metrics``.
The fields cover the three axes the drift detector (future Fase 3) will
watch:

* **size** — files, chunks, entities, relations, graphify nodes/edges;
* **honesty** — confidence distribution of graphify edges (EXTRACTED vs
  INFERRED vs AMBIGUOUS) plus cross-repo bridge count;
* **topology** — weakly connected node count, community count, and the top
  10 god nodes so a later comparison can tell when the "core" of the
  project has shifted.

The collector never raises on partial failure: when Neo4j is missing the
graphify-side fields stay null and the row is still written, so an operator
can tell "Postgres fine, Neo4j down" apart from "collector never ran".
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN


VALID_SOURCES = ("gpc-index", "graph-bridge", "manual", "snapshot")


@dataclass
class SnapshotResult:
    id: str
    project_slug: str
    source: str
    counts: dict[str, int | None]
    god_nodes_top10: list[dict[str, Any]]
    metadata: dict[str, Any]


def collect_metrics(
    *,
    project_slug: str,
    source: str = "snapshot",
) -> SnapshotResult:
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES!r}, got {source!r}")

    pg = _postgres_counts(project_slug)
    neo = _neo4j_counts(project_slug)
    project_id = pg.pop("_project_id", None)
    god_nodes = neo.get("god_nodes_top10", []) or []

    with psycopg.connect(POSTGRES_DSN) as conn:
        row = conn.execute(
            """
            insert into gpc_self_metrics (
                project_id, project_slug, source,
                files_count, chunks_count, entities_count, relations_count,
                graphify_projects, graphify_repos, graphify_nodes,
                graphify_edges_same_repo, graphify_edges_cross_repo,
                cross_repo_bridges,
                extracted_count, inferred_count, ambiguous_count,
                weakly_connected_nodes, community_count, god_nodes_top10,
                max_file_age_hours, metadata
            )
            values (
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            returning id::text
            """,
            (
                project_id,
                project_slug,
                source,
                pg.get("files_count"),
                pg.get("chunks_count"),
                pg.get("entities_count"),
                pg.get("relations_count"),
                neo.get("graphify_projects"),
                neo.get("graphify_repos"),
                neo.get("graphify_nodes"),
                neo.get("graphify_edges_same_repo"),
                neo.get("graphify_edges_cross_repo"),
                neo.get("cross_repo_bridges"),
                neo.get("extracted_count"),
                neo.get("inferred_count"),
                neo.get("ambiguous_count"),
                neo.get("weakly_connected_nodes"),
                neo.get("community_count"),
                Jsonb(god_nodes),
                pg.get("max_file_age_hours"),
                Jsonb({"neo4j_available": neo.get("available", False)}),
            ),
        ).fetchone()

    return SnapshotResult(
        id=row[0],
        project_slug=project_slug,
        source=source,
        counts={**pg, **{k: v for k, v in neo.items() if k != "god_nodes_top10"}},
        god_nodes_top10=god_nodes,
        metadata={"neo4j_available": neo.get("available", False)},
    )


def _postgres_counts(project_slug: str) -> dict[str, Any]:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        proj = conn.execute(
            "select id::text as id from gpc_projects where slug = %s",
            (project_slug,),
        ).fetchone()
        if not proj:
            return {"_project_id": None}
        project_id = proj["id"]
        row = conn.execute(
            """
            select
                (select count(*) from gpc_files where project_id = %s)    as files_count,
                (select count(*) from gpc_chunks where project_id = %s)   as chunks_count,
                (select count(*) from gpc_entities where project_id = %s) as entities_count,
                (select count(*) from gpc_relations where project_id = %s) as relations_count,
                (
                    select extract(epoch from (now() - min(updated_at))) / 3600
                    from gpc_files where project_id = %s
                ) as max_file_age_hours
            """,
            (project_id, project_id, project_id, project_id, project_id),
        ).fetchone()
        return {
            "_project_id": project_id,
            "files_count": int(row["files_count"] or 0),
            "chunks_count": int(row["chunks_count"] or 0),
            "entities_count": int(row["entities_count"] or 0),
            "relations_count": int(row["relations_count"] or 0),
            "max_file_age_hours": (
                int(row["max_file_age_hours"]) if row["max_file_age_hours"] is not None else None
            ),
        }


def _neo4j_counts(project_slug: str) -> dict[str, Any]:
    """Collect graphify-side counts, tolerating Neo4j being unavailable."""

    try:
        from gpc.graph import neo4j_driver
    except Exception:  # noqa: BLE001
        return {"available": False}

    try:
        with neo4j_driver() as driver:
            with driver.session() as session:
                presence = session.run(
                    "MATCH (p:GraphifyProject {slug: $s}) RETURN count(p) AS c",
                    s=project_slug,
                ).single()
                has_project = bool(presence and presence["c"])

                counts = session.run(
                    """
                    CALL () {
                        MATCH (p:GraphifyProject {slug: $s})
                        RETURN count(p) AS graphify_projects
                    }
                    CALL () {
                        MATCH (r:GraphifyRepo {project_slug: $s})
                        RETURN count(r) AS graphify_repos
                    }
                    CALL () {
                        MATCH (n:GraphifyNode {project_slug: $s})
                        RETURN count(n) AS graphify_nodes
                    }
                    CALL () {
                        MATCH (ra:GraphifyRepo)-[:HAS_GRAPH_NODE]->(a:GraphifyNode)
                              -[:GRAPHIFY_RELATION]->
                              (b:GraphifyNode)<-[:HAS_GRAPH_NODE]-(rb:GraphifyRepo)
                        WHERE a.project_slug = $s
                          AND ra.slug = rb.slug
                        RETURN count(*) AS graphify_edges_same_repo
                    }
                    CALL () {
                        MATCH (ra:GraphifyRepo)-[:HAS_GRAPH_NODE]->(a:GraphifyNode)
                              -[:GRAPHIFY_RELATION]->
                              (b:GraphifyNode)<-[:HAS_GRAPH_NODE]-(rb:GraphifyRepo)
                        WHERE a.project_slug = $s
                          AND ra.slug <> rb.slug
                        RETURN count(*) AS graphify_edges_cross_repo
                    }
                    CALL () {
                        MATCH (:GraphifyNode {project_slug: $s})
                              -[r:CROSS_REPO_BRIDGE]->
                              (:GraphifyNode {project_slug: $s})
                        RETURN count(r) AS cross_repo_bridges
                    }
                    RETURN graphify_projects, graphify_repos, graphify_nodes,
                           graphify_edges_same_repo, graphify_edges_cross_repo,
                           cross_repo_bridges
                    """,
                    s=project_slug,
                ).single()

                confidence_rows = session.run(
                    """
                    MATCH (:GraphifyNode {project_slug: $s})
                          -[r:CROSS_REPO_BRIDGE]->
                          (:GraphifyNode)
                    RETURN coalesce(r.confidence, 'INFERRED') AS c, count(*) AS n
                    UNION ALL
                    MATCH (:GraphifyNode {project_slug: $s})
                          -[r:GRAPHIFY_RELATION]->
                          (:GraphifyNode)
                    RETURN 'EXTRACTED' AS c, count(*) AS n
                    """,
                    s=project_slug,
                ).data()

                conf = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
                for r in confidence_rows:
                    conf[r["c"]] = conf.get(r["c"], 0) + int(r["n"] or 0)

                weakly = session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $s})
                    WHERE NOT EXISTS {
                        (n)-[:GRAPHIFY_RELATION|CROSS_REPO_BRIDGE]-()
                    }
                    RETURN count(n) AS c
                    """,
                    s=project_slug,
                ).single()

                communities = session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $s})
                    WHERE n.community IS NOT NULL
                    RETURN count(DISTINCT n.community) AS c
                    """,
                    s=project_slug,
                ).single()

                god_nodes = session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $s})
                    WITH n, size([(n)--() | 1]) AS degree
                    WHERE degree > 0
                    RETURN n.id AS id, n.label AS label,
                           n.repo_slug AS repo, degree
                    ORDER BY degree DESC, label ASC
                    LIMIT 10
                    """,
                    s=project_slug,
                ).data()

        return {
            "available": True,
            "graphify_projects": int(counts["graphify_projects"] or 0) if counts else 0,
            "graphify_repos": int(counts["graphify_repos"] or 0) if counts else 0,
            "graphify_nodes": int(counts["graphify_nodes"] or 0) if counts else 0,
            "graphify_edges_same_repo": int(counts["graphify_edges_same_repo"] or 0) if counts else 0,
            "graphify_edges_cross_repo": int(counts["graphify_edges_cross_repo"] or 0) if counts else 0,
            "cross_repo_bridges": int(counts["cross_repo_bridges"] or 0) if counts else 0,
            "extracted_count": conf["EXTRACTED"],
            "inferred_count": conf["INFERRED"],
            "ambiguous_count": conf["AMBIGUOUS"],
            "weakly_connected_nodes": int(weakly["c"] or 0) if weakly else 0,
            "community_count": int(communities["c"] or 0) if communities else 0,
            "god_nodes_top10": god_nodes or [],
        }
    except Exception:  # noqa: BLE001 — never block the caller
        return {"available": False}


def list_snapshots(
    *,
    project_slug: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        if project_slug:
            return conn.execute(
                """
                select id::text as id, collected_at, project_slug, source,
                       files_count, chunks_count, entities_count, relations_count,
                       graphify_nodes, graphify_edges_cross_repo, cross_repo_bridges,
                       extracted_count, inferred_count, ambiguous_count,
                       weakly_connected_nodes, community_count,
                       god_nodes_top10, metadata
                from gpc_self_metrics
                where project_slug = %s
                order by collected_at desc
                limit %s
                """,
                (project_slug, limit),
            ).fetchall()
        return conn.execute(
            """
            select id::text as id, collected_at, project_slug, source,
                   files_count, chunks_count, entities_count, relations_count,
                   graphify_nodes, graphify_edges_cross_repo, cross_repo_bridges,
                   extracted_count, inferred_count, ambiguous_count,
                   weakly_connected_nodes, community_count,
                   god_nodes_top10, metadata
            from gpc_self_metrics
            order by collected_at desc
            limit %s
            """,
            (limit,),
        ).fetchall()


def fetch_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            """
            select id::text as id, collected_at, project_slug, source,
                   files_count, chunks_count, entities_count, relations_count,
                   graphify_nodes, graphify_edges_same_repo,
                   graphify_edges_cross_repo, cross_repo_bridges,
                   extracted_count, inferred_count, ambiguous_count,
                   weakly_connected_nodes, community_count,
                   god_nodes_top10, max_file_age_hours, metadata
            from gpc_self_metrics
            where id = %s
            """,
            (snapshot_id,),
        ).fetchone()


def fetch_pair(
    *,
    project_slug: str,
    window_hours: int | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return two snapshots to diff.

    If ``from_id`` + ``to_id`` are supplied, fetch those exact rows.
    Otherwise pick the most recent snapshot as ``to`` and the most recent
    snapshot older than ``window_hours`` ago as ``from``. ``window_hours``
    defaults to 24.
    """

    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        if from_id and to_id:
            a = conn.execute(
                "select * from gpc_self_metrics where id = %s",
                (from_id,),
            ).fetchone()
            b = conn.execute(
                "select * from gpc_self_metrics where id = %s",
                (to_id,),
            ).fetchone()
            return a, b

        window = window_hours if window_hours is not None else 24
        window = max(1, min(int(window), 24 * 365))
        to = conn.execute(
            """
            select * from gpc_self_metrics
            where project_slug = %s
            order by collected_at desc
            limit 1
            """,
            (project_slug,),
        ).fetchone()
        if not to:
            return None, None
        frm = conn.execute(
            """
            select * from gpc_self_metrics
            where project_slug = %s
              and collected_at <= now() - (%s::text || ' hours')::interval
            order by collected_at desc
            limit 1
            """,
            (project_slug, str(window)),
        ).fetchone()
        return frm, to
