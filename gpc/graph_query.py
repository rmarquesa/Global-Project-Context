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
                WHERE n.id = $q OR toLower(n.label) CONTAINS toLower($q)
                RETURN n
                ORDER BY CASE WHEN n.id = $q THEN 0 ELSE 1 END,
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

            gods = session.run(
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
                top_k=top_k_gods,
            ).data()

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
