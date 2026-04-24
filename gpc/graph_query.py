"""Structural queries on the Neo4j graph.

These functions power the ``gpc.graph_neighbors``, ``gpc.graph_summary`` and
``gpc.graph_path`` MCP tools. They are intentionally thin wrappers around
Cypher so that every result is explicit about:

* which project it belongs to (``project_slug`` scope is mandatory);
* the relation type and direction of each edge;
* the ``confidence`` and ``confidence_score`` attached to edges, so the
  caller can reason about how trustworthy each claim is.

GRAPHIFY_RELATION edges produced by Graphify today do not carry a confidence
property — they are treated as ``EXTRACTED`` structural facts. CROSS_REPO_BRIDGE
edges always carry ``INFERRED`` or ``AMBIGUOUS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gpc.cross_repo import is_generic_label
from gpc.graph import neo4j_driver


CONFIDENCE_SCORES = {
    "EXTRACTED": 1.0,
    "INFERRED": 0.5,
    "AMBIGUOUS": 0.2,
}

DEFAULT_MIN_CONFIDENCE = "EXTRACTED"
ALLOWED_CONFIDENCES = ("EXTRACTED", "INFERRED", "AMBIGUOUS")


def _min_score(min_confidence: str) -> float:
    if min_confidence not in ALLOWED_CONFIDENCES:
        raise ValueError(
            f"min_confidence must be one of {ALLOWED_CONFIDENCES!r}, got {min_confidence!r}"
        )
    return CONFIDENCE_SCORES[min_confidence]


def _edge_passes(edge: dict[str, Any], threshold: float) -> bool:
    conf = edge.get("confidence")
    score = edge.get("confidence_score")
    if conf is None and score is None:
        # GRAPHIFY_RELATION edges lack explicit confidence; treat as EXTRACTED.
        return threshold <= 1.0
    if score is not None:
        return float(score) >= threshold
    return CONFIDENCE_SCORES.get(conf or "AMBIGUOUS", 0.0) >= threshold


@dataclass
class NeighborEdge:
    relation: str
    confidence: str | None
    confidence_score: float | None
    rule: str | None = None
    evidence: str | None = None
    direction: str = "out"  # "out" or "in" relative to the start node


@dataclass
class NeighborNode:
    id: str
    label: str
    repo_slug: str | None
    source_file: str | None
    hops: int
    edges: list[NeighborEdge] = field(default_factory=list)


def graph_neighbors(
    project_slug: str,
    node_query: str,
    *,
    depth: int = 1,
    min_confidence: str = DEFAULT_MIN_CONFIDENCE,
    relations: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return neighbors of a node in a project's Graphify subgraph.

    ``node_query`` is matched against ``GraphifyNode.id`` for an exact match
    or against ``GraphifyNode.label`` with case-insensitive containment. The
    first match wins — callers can inspect the returned ``start`` node to
    confirm which one was picked.

    ``depth`` is capped at 3. ``relations`` filters by relationship type
    (e.g. ``["GRAPHIFY_RELATION", "CROSS_REPO_BRIDGE"]``); defaults to both.
    """

    if not project_slug:
        raise ValueError("project_slug is required")
    if depth < 1 or depth > 3:
        raise ValueError("depth must be between 1 and 3")
    threshold = _min_score(min_confidence)
    rel_filter = [r.upper() for r in relations] if relations else None

    with neo4j_driver() as driver:
        with driver.session() as session:
            start_record = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug})
                WHERE n.id = $q
                   OR toLower(n.label) CONTAINS toLower($q)
                   OR toLower(coalesce(n.source_file, '')) = toLower($q)
                RETURN n
                ORDER BY CASE
                             WHEN n.id = $q THEN 0
                             WHEN toLower(coalesce(n.source_file, '')) = toLower($q) THEN 1
                             ELSE 2
                         END,
                         size(n.label), n.id
                LIMIT 1
                """,
                slug=project_slug,
                q=node_query,
            ).single()
            if not start_record:
                return {
                    "project_slug": project_slug,
                    "query": node_query,
                    "start": None,
                    "neighbors": [],
                }
            start = dict(start_record["n"])

            # Expand up to `depth` hops. We return each intermediate and final
            # neighbor with the sequence of edges that led to it so the caller
            # can reconstruct paths. Restrict to semantic edges only — HAS_REPO
            # / HAS_GRAPH_NODE / OWNS_REPO would leak structural plumbing.
            rows = session.run(
                f"""
                MATCH (start:GraphifyNode {{id: $start_id}})
                MATCH path = (start)-[rels:GRAPHIFY_RELATION|CROSS_REPO_BRIDGE*1..{depth}]-(nb:GraphifyNode)
                WHERE nb.project_slug = $slug
                  AND nb.id <> start.id
                  AND all(n IN nodes(path) WHERE n:GraphifyNode AND n.project_slug = $slug)
                WITH start, nb, rels, path,
                     [r IN rels | {{
                         relation: type(r),
                         confidence: r.confidence,
                         confidence_score: r.confidence_score,
                         rule: r.rule,
                         evidence: r.evidence,
                         startNode: startNode(r).id,
                         endNode: endNode(r).id
                     }}] AS edges
                RETURN nb.id AS id, nb.label AS label, nb.repo_slug AS repo_slug,
                       nb.source_file AS source_file, length(path) AS hops, edges
                ORDER BY hops ASC, label ASC
                LIMIT $limit
                """,
                start_id=start["id"],
                slug=project_slug,
                limit=limit * 4,  # fetch extras to survive confidence/relation filtering
            ).data()

    kept: list[NeighborNode] = []
    seen: set[str] = set()
    for row in rows:
        # Apply relation-type + confidence filter across every edge in the path.
        edges_raw = row["edges"] or []
        if rel_filter is not None and not all(e["relation"] in rel_filter for e in edges_raw):
            continue
        if not all(_edge_passes(e, threshold) for e in edges_raw):
            continue
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        hop_edges: list[NeighborEdge] = []
        prev_id = start["id"]
        for edge in edges_raw:
            direction = "out" if edge.get("startNode") == prev_id else "in"
            hop_edges.append(
                NeighborEdge(
                    relation=edge["relation"],
                    confidence=edge.get("confidence"),
                    confidence_score=edge.get("confidence_score"),
                    rule=edge.get("rule"),
                    evidence=edge.get("evidence"),
                    direction=direction,
                )
            )
            prev_id = edge.get("endNode") if direction == "out" else edge.get("startNode")
        kept.append(
            NeighborNode(
                id=row["id"],
                label=row["label"],
                repo_slug=row.get("repo_slug"),
                source_file=row.get("source_file"),
                hops=int(row["hops"]),
                edges=hop_edges,
            )
        )
        if len(kept) >= limit:
            break

    return {
        "project_slug": project_slug,
        "query": node_query,
        "start": {
            "id": start["id"],
            "label": start["label"],
            "repo_slug": start.get("repo_slug"),
            "source_file": start.get("source_file"),
        },
        "depth": depth,
        "min_confidence": min_confidence,
        "relations_filter": rel_filter,
        "neighbors": [
            {
                "id": n.id,
                "label": n.label,
                "repo_slug": n.repo_slug,
                "source_file": n.source_file,
                "hops": n.hops,
                "edges": [
                    {
                        "relation": e.relation,
                        "confidence": e.confidence,
                        "confidence_score": e.confidence_score,
                        "rule": e.rule,
                        "evidence": e.evidence,
                        "direction": e.direction,
                    }
                    for e in n.edges
                ],
            }
            for n in kept
        ],
    }


def graph_summary(
    project_slug: str,
    *,
    top_k_gods: int = 10,
    include_cohesion: bool = True,
) -> dict[str, Any]:
    """Return god nodes, repo breakdown, bridges summary and communities.

    This is the structured equivalent of ``graphify-out/GRAPH_REPORT.md`` —
    served over MCP so the caller doesn't have to ingest the full markdown.
    """

    if not project_slug:
        raise ValueError("project_slug is required")

    with neo4j_driver() as driver:
        with driver.session() as session:
            project = session.run(
                "MATCH (p:GraphifyProject {slug: $slug}) RETURN p.slug AS slug, p.updated_at AS updated_at",
                slug=project_slug,
            ).single()
            if not project:
                return {"project_slug": project_slug, "found": False}

            repos = session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})-[:HAS_REPO]->(r:GraphifyRepo)
                OPTIONAL MATCH (r)-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                RETURN r.slug AS repo,
                       r.root_path AS root_path,
                       count(n) AS node_count,
                       count(DISTINCT n.community) AS community_count
                ORDER BY repo
                """,
                slug=project_slug,
            ).data()

            # Fetch 3x top_k so we can split between "central" (distinctive
            # labels that reflect real architectural hubs) and "utility_hub"
            # (generic labels like `run()`, `main()`, `connect()` that earn
            # high degree by being dispatch/bootstrap code). Callers get both
            # buckets and can decide which one to show the model.
            candidates = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug})
                WITH n, size([(n)--() | 1]) AS degree
                WHERE degree > 0
                RETURN n.id AS id, n.label AS label, n.repo_slug AS repo,
                       n.file_type AS file_type, n.source_file AS source_file,
                       degree
                ORDER BY degree DESC, label ASC
                LIMIT $top_k
                """,
                slug=project_slug,
                top_k=top_k_gods * 3,
            ).data()
            gods_central: list[dict[str, Any]] = []
            gods_utility: list[dict[str, Any]] = []
            for row in candidates:
                target = gods_utility if is_generic_label(row.get("label")) else gods_central
                if len(target) < top_k_gods:
                    target.append(row)
                if len(gods_central) >= top_k_gods and len(gods_utility) >= top_k_gods:
                    break
            # Keep the original ``god_nodes`` field (now the "central" list)
            # for backwards compatibility with existing callers; expose the
            # utility list as ``utility_hubs`` alongside.
            gods = gods_central

            bridges = session.run(
                """
                MATCH (a:GraphifyNode {project_slug: $slug})
                      -[r:CROSS_REPO_BRIDGE]->
                      (b:GraphifyNode {project_slug: $slug})
                RETURN r.rule AS rule,
                       r.confidence AS confidence,
                       count(*) AS count
                ORDER BY count DESC
                """,
                slug=project_slug,
            ).data()

            relation_mix = session.run(
                """
                MATCH (:GraphifyNode {project_slug: $slug})-[r:GRAPHIFY_RELATION]->(:GraphifyNode)
                RETURN r.relation AS relation, count(*) AS count
                ORDER BY count DESC
                LIMIT 15
                """,
                slug=project_slug,
            ).data()

            cohesion_rows: list[dict[str, Any]] = []
            if include_cohesion:
                cohesion_rows = session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $slug})
                    WHERE n.community IS NOT NULL
                    WITH n.community AS community, collect(n) AS members
                    WITH community, size(members) AS node_count,
                         [m IN members | m.repo_slug] AS repos,
                         [m IN members | m.label][0..4] AS sample_labels
                    RETURN community, node_count,
                           size(apoc.coll.toSet(repos)) AS repo_count,
                           sample_labels
                    ORDER BY node_count DESC
                    LIMIT 20
                    """,
                    slug=project_slug,
                ).data() if _has_apoc(session) else session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $slug})
                    WHERE n.community IS NOT NULL
                    WITH n.community AS community, collect(n) AS members
                    WITH community, size(members) AS node_count,
                         members
                    RETURN community, node_count,
                           size(collect(DISTINCT [m IN members | m.repo_slug])) AS repo_count,
                           [m IN members | m.label][0..4] AS sample_labels
                    ORDER BY node_count DESC
                    LIMIT 20
                    """,
                    slug=project_slug,
                ).data()

    return {
        "project_slug": project_slug,
        "found": True,
        "repos": repos,
        "god_nodes": gods,
        "utility_hubs": gods_utility,
        "cross_repo_bridges": bridges,
        "relation_mix": relation_mix,
        "communities": cohesion_rows,
    }


def _has_apoc(session) -> bool:
    try:
        session.run("RETURN apoc.version()").single()
        return True
    except Exception:
        return False


def graph_diff(
    project_slug: str,
    *,
    window_hours: int | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
) -> dict[str, Any]:
    """Diff two ``gpc_self_metrics`` snapshots for a project.

    Default: compares the latest snapshot to the most recent one older than
    ``window_hours`` ago (default 24). Pass ``from_id`` / ``to_id`` for an
    exact pair — useful for programmatic drift detection.

    The return payload highlights three axes the caller care about:
        * ``numeric_deltas`` — raw count changes (files, nodes, bridges…);
        * ``god_nodes_diff`` — which labels entered / exited the top 10;
        * ``confidence_shift`` — movement in the EXTRACTED/INFERRED/AMBIGUOUS
          distribution, expressed both in absolute counts and in percentage
          points.
    """

    if not project_slug:
        raise ValueError("project_slug is required")

    # Imported lazily so graph_query stays importable without the collector
    # schema being present (older deployments).
    from gpc.self_metrics import fetch_pair

    frm, to = fetch_pair(
        project_slug=project_slug,
        window_hours=window_hours,
        from_id=from_id,
        to_id=to_id,
    )
    if not to:
        return {
            "project_slug": project_slug,
            "found": False,
            "reason": "no_snapshots_for_project",
        }
    if not frm:
        return {
            "project_slug": project_slug,
            "found": False,
            "reason": "no_prior_snapshot_in_window",
            "latest": _snapshot_view(to),
        }

    numeric_fields = (
        "files_count",
        "chunks_count",
        "entities_count",
        "relations_count",
        "graphify_projects",
        "graphify_repos",
        "graphify_nodes",
        "graphify_edges_same_repo",
        "graphify_edges_cross_repo",
        "cross_repo_bridges",
        "extracted_count",
        "inferred_count",
        "ambiguous_count",
        "weakly_connected_nodes",
        "community_count",
    )
    numeric_deltas = {
        field: {
            "before": frm.get(field),
            "after": to.get(field),
            "delta": _delta(to.get(field), frm.get(field)),
        }
        for field in numeric_fields
    }

    god_from = {g.get("label") for g in (frm.get("god_nodes_top10") or []) if g.get("label")}
    god_to = {g.get("label") for g in (to.get("god_nodes_top10") or []) if g.get("label")}
    god_diff = {
        "entered": sorted(god_to - god_from),
        "exited": sorted(god_from - god_to),
        "stable": sorted(god_from & god_to),
    }

    confidence_shift = _confidence_shift(frm, to)

    return {
        "project_slug": project_slug,
        "found": True,
        "from": _snapshot_view(frm),
        "to": _snapshot_view(to),
        "numeric_deltas": numeric_deltas,
        "god_nodes_diff": god_diff,
        "confidence_shift": confidence_shift,
    }


def _delta(after: Any, before: Any) -> int | None:
    if after is None or before is None:
        return None
    try:
        return int(after) - int(before)
    except (TypeError, ValueError):
        return None


def _snapshot_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "collected_at": str(row.get("collected_at")),
        "source": row.get("source"),
    }


def _confidence_shift(frm: dict[str, Any], to: dict[str, Any]) -> dict[str, Any]:
    def _totals(row: dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(row.get("extracted_count") or 0),
            int(row.get("inferred_count") or 0),
            int(row.get("ambiguous_count") or 0),
        )

    e_from, i_from, a_from = _totals(frm)
    e_to, i_to, a_to = _totals(to)
    total_from = max(1, e_from + i_from + a_from)
    total_to = max(1, e_to + i_to + a_to)

    def _pct(count: int, total: int) -> float:
        return round((count / total) * 100, 2)

    return {
        "before_pct": {
            "EXTRACTED": _pct(e_from, total_from),
            "INFERRED": _pct(i_from, total_from),
            "AMBIGUOUS": _pct(a_from, total_from),
        },
        "after_pct": {
            "EXTRACTED": _pct(e_to, total_to),
            "INFERRED": _pct(i_to, total_to),
            "AMBIGUOUS": _pct(a_to, total_to),
        },
        "delta_pp": {
            "EXTRACTED": round(_pct(e_to, total_to) - _pct(e_from, total_from), 2),
            "INFERRED": round(_pct(i_to, total_to) - _pct(i_from, total_from), 2),
            "AMBIGUOUS": round(_pct(a_to, total_to) - _pct(a_from, total_from), 2),
        },
    }


def graph_community(
    project_slug: str,
    community_id: int,
    *,
    top_members: int = 20,
    top_external_bridges: int = 10,
) -> dict[str, Any]:
    """Drill into one Graphify community: members, repos, external reach.

    Communities come from Graphify's own clustering (``GraphifyNode.community``
    is the community id within the repo). For a full-project view we report:

    * a sample of the most-connected members, sorted by degree;
    * the repos that participate and how many nodes each contributes;
    * cross-community edges (both `GRAPHIFY_RELATION` and `CROSS_REPO_BRIDGE`)
      so the caller can see how the community talks to the rest of the graph.
    """

    if not project_slug:
        raise ValueError("project_slug is required")

    with neo4j_driver() as driver:
        with driver.session() as session:
            meta = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug, community: $cid})
                RETURN count(n) AS total,
                       collect(DISTINCT n.repo_slug) AS repos
                """,
                slug=project_slug, cid=community_id,
            ).single()
            if not meta or (meta["total"] or 0) == 0:
                return {
                    "project_slug": project_slug,
                    "community_id": community_id,
                    "found": False,
                }

            members = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug, community: $cid})
                WITH n, size([(n)--() | 1]) AS degree
                RETURN n.id AS id, n.label AS label, n.repo_slug AS repo,
                       n.file_type AS file_type, n.source_file AS source_file,
                       degree
                ORDER BY degree DESC, label ASC
                LIMIT $limit
                """,
                slug=project_slug, cid=community_id, limit=top_members,
            ).data()

            repo_breakdown = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug, community: $cid})
                RETURN n.repo_slug AS repo, count(*) AS nodes
                ORDER BY nodes DESC
                """,
                slug=project_slug, cid=community_id,
            ).data()

            external = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug, community: $cid})
                      -[r:GRAPHIFY_RELATION|CROSS_REPO_BRIDGE]-
                      (other:GraphifyNode {project_slug: $slug})
                WHERE other.community IS NULL OR other.community <> $cid
                RETURN type(r) AS relation,
                       r.confidence AS confidence,
                       r.confidence_score AS confidence_score,
                       r.rule AS rule,
                       n.label AS from_label,
                       n.repo_slug AS from_repo,
                       other.label AS to_label,
                       other.repo_slug AS to_repo,
                       other.community AS to_community
                ORDER BY coalesce(r.confidence_score, 0.0) DESC
                LIMIT $limit
                """,
                slug=project_slug, cid=community_id, limit=top_external_bridges,
            ).data()

    return {
        "project_slug": project_slug,
        "community_id": community_id,
        "found": True,
        "size": int(meta["total"] or 0),
        "repos": [r for r in (meta["repos"] or []) if r],
        "repo_breakdown": repo_breakdown,
        "members": members,
        "external_bridges": external,
    }


def graph_path(
    project_slug: str,
    a_query: str,
    b_query: str,
    *,
    max_hops: int = 6,
    min_confidence: str = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Return the shortest path between two nodes in a project.

    Each hop in the result carries the relation type and confidence so the
    caller can judge whether the connection is structural (``EXTRACTED``) or
    heuristic (``INFERRED`` / ``AMBIGUOUS``).
    """

    if not project_slug:
        raise ValueError("project_slug is required")
    if max_hops < 1 or max_hops > 8:
        raise ValueError("max_hops must be between 1 and 8")
    threshold = _min_score(min_confidence)

    with neo4j_driver() as driver:
        with driver.session() as session:
            resolved = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug})
                WHERE n.id = $q OR toLower(n.label) CONTAINS toLower($q)
                RETURN n ORDER BY CASE WHEN n.id = $q THEN 0 ELSE 1 END,
                                  size(n.label), n.id
                LIMIT 1
                """,
                slug=project_slug, q=a_query,
            ).single()
            a_node = dict(resolved["n"]) if resolved else None

            resolved = session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $slug})
                WHERE n.id = $q OR toLower(n.label) CONTAINS toLower($q)
                RETURN n ORDER BY CASE WHEN n.id = $q THEN 0 ELSE 1 END,
                                  size(n.label), n.id
                LIMIT 1
                """,
                slug=project_slug, q=b_query,
            ).single()
            b_node = dict(resolved["n"]) if resolved else None

            if not a_node or not b_node:
                return {
                    "project_slug": project_slug,
                    "a_query": a_query,
                    "b_query": b_query,
                    "a": a_node and {"id": a_node["id"], "label": a_node["label"]},
                    "b": b_node and {"id": b_node["id"], "label": b_node["label"]},
                    "path": None,
                    "reason": "node_not_found",
                }

            record = session.run(
                f"""
                MATCH (a:GraphifyNode {{id: $a_id}}),
                      (b:GraphifyNode {{id: $b_id}})
                MATCH path = shortestPath((a)-[:GRAPHIFY_RELATION|CROSS_REPO_BRIDGE*..{max_hops}]-(b))
                WHERE all(n IN nodes(path) WHERE n:GraphifyNode AND n.project_slug = $slug)
                RETURN nodes(path) AS ns,
                       [r IN relationships(path) | {{
                           relation: type(r),
                           confidence: r.confidence,
                           confidence_score: r.confidence_score,
                           rule: r.rule,
                           evidence: r.evidence,
                           startNode: startNode(r).id,
                           endNode: endNode(r).id
                       }}] AS rels
                """,
                a_id=a_node["id"],
                b_id=b_node["id"],
                slug=project_slug,
            ).single()

    if not record:
        return {
            "project_slug": project_slug,
            "a_query": a_query,
            "b_query": b_query,
            "a": {"id": a_node["id"], "label": a_node["label"]},
            "b": {"id": b_node["id"], "label": b_node["label"]},
            "path": None,
            "reason": "no_path_within_max_hops",
        }

    rels = record["rels"] or []
    ns = record["ns"] or []

    if not all(_edge_passes(r, threshold) for r in rels):
        return {
            "project_slug": project_slug,
            "a_query": a_query,
            "b_query": b_query,
            "a": {"id": a_node["id"], "label": a_node["label"]},
            "b": {"id": b_node["id"], "label": b_node["label"]},
            "path": None,
            "reason": "path_below_min_confidence",
            "rejected_path": _serialise_path(ns, rels),
        }

    return {
        "project_slug": project_slug,
        "a_query": a_query,
        "b_query": b_query,
        "a": {"id": a_node["id"], "label": a_node["label"]},
        "b": {"id": b_node["id"], "label": b_node["label"]},
        "min_confidence": min_confidence,
        "max_hops": max_hops,
        "path": _serialise_path(ns, rels),
    }


def _serialise_path(nodes: list[Any], rels: list[dict[str, Any]]) -> dict[str, Any]:
    hops = []
    for i, rel in enumerate(rels):
        frm = dict(nodes[i])
        to = dict(nodes[i + 1])
        direction = "out" if rel.get("startNode") == frm.get("id") else "in"
        hops.append(
            {
                "from": {"id": frm["id"], "label": frm["label"], "repo_slug": frm.get("repo_slug")},
                "to": {"id": to["id"], "label": to["label"], "repo_slug": to.get("repo_slug")},
                "relation": rel["relation"],
                "direction": direction,
                "confidence": rel.get("confidence"),
                "confidence_score": rel.get("confidence_score"),
                "rule": rel.get("rule"),
                "evidence": rel.get("evidence"),
            }
        )
    return {
        "length": len(hops),
        "nodes": [
            {
                "id": dict(n)["id"],
                "label": dict(n)["label"],
                "repo_slug": dict(n).get("repo_slug"),
            }
            for n in nodes
        ],
        "hops": hops,
    }
