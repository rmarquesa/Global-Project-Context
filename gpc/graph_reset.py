"""Reset helpers for the Neo4j projection.

``graph-reset`` is destructive on Neo4j. It never touches Postgres — Postgres
is the source of truth and can rebuild Neo4j on demand via
``project_graph_to_neo4j()`` + ``build_bridges()``.
"""

from __future__ import annotations

from dataclasses import dataclass

from gpc.cross_repo import (
    DEFAULT_RULES,
    build_bridges,
    list_graphify_projects,
)
from gpc.graph import neo4j_driver, project_graph_to_neo4j


@dataclass
class ResetStats:
    neo4j_nodes_deleted: int = 0
    neo4j_relationships_deleted: int = 0
    gpc_rebuilt: bool = False
    graphify_projects_bridged: int = 0
    bridges_written: int = 0


def reset_neo4j(*, project_slug: str | None = None) -> ResetStats:
    """Delete the Neo4j projection.

    When ``project_slug`` is provided, only nodes scoped to that slug are
    removed. When omitted, every GPC- and Graphify-owned label is wiped.
    """

    stats = ResetStats()
    with neo4j_driver() as driver:
        with driver.session() as session:
            if project_slug:
                result = session.run(
                    """
                    MATCH (n)
                    WHERE (n:GraphifyProject AND n.slug = $slug)
                       OR (n:GraphifyRepo AND n.project_slug = $slug)
                       OR (n:GraphifyNode AND n.project_slug = $slug)
                       OR (n:GPCProject AND n.slug = $slug)
                       OR (n:GPCRepo AND n.project_slug = $slug)
                       OR (n:GPCEntity AND n.project_slug = $slug)
                    WITH n, size([ (n)-[r]-() | r ]) AS rels
                    WITH collect({rels: rels}) AS rows,
                         collect(n) AS nodes
                    CALL (nodes) {
                        UNWIND nodes AS node
                        DETACH DELETE node
                    }
                    RETURN
                        size(nodes) AS nodes_deleted,
                        reduce(s=0, r IN rows | s + r.rels) AS rels_deleted
                    """,
                    slug=project_slug,
                ).single()
            else:
                result = session.run(
                    """
                    MATCH (n)
                    WHERE any(lbl IN labels(n) WHERE lbl IN [
                        'GraphifyProject','GraphifyRepo','GraphifyNode',
                        'GPCProject','GPCRepo','GPCEntity'
                    ])
                    WITH n, size([ (n)-[r]-() | r ]) AS rels
                    WITH collect({rels: rels}) AS rows,
                         collect(n) AS nodes
                    CALL (nodes) {
                        UNWIND nodes AS node
                        DETACH DELETE node
                    }
                    RETURN
                        size(nodes) AS nodes_deleted,
                        reduce(s=0, r IN rows | s + r.rels) AS rels_deleted
                    """
                ).single()
    if result:
        stats.neo4j_nodes_deleted = int(result["nodes_deleted"] or 0)
        stats.neo4j_relationships_deleted = int(result["rels_deleted"] or 0)
    return stats


def rebuild_gpc_projection() -> None:
    project_graph_to_neo4j(projection_name="reset_rebuild")


def rebuild_bridges(project_slug: str | None = None) -> tuple[int, int]:
    """Run bridging for one project or every Graphify project found in Neo4j."""

    slugs = [project_slug] if project_slug else list_graphify_projects()
    total_projects = 0
    total_edges = 0
    for slug in slugs:
        if not slug:
            continue
        stats = build_bridges(slug, rules=DEFAULT_RULES, clear_existing=False)
        if stats.repos >= 2:
            total_projects += 1
            total_edges += stats.edges_written
    return total_projects, total_edges


def reset_and_rebuild(
    *,
    project_slug: str | None = None,
    rebuild_gpc: bool = True,
    rebuild_graphify_bridges: bool = True,
) -> ResetStats:
    stats = reset_neo4j(project_slug=project_slug)
    if rebuild_gpc:
        rebuild_gpc_projection()
        stats.gpc_rebuilt = True
    if rebuild_graphify_bridges:
        # Graphify subgraph lives behind the post-commit hook; resetting Neo4j
        # wipes it. Rebuilding from here would require re-running graphify in
        # every repo, which is out of scope for a read-only rebuild step.
        # We only rebuild bridges for whatever projection is *still* in Neo4j.
        bridged, edges = rebuild_bridges(project_slug=project_slug)
        stats.graphify_projects_bridged = bridged
        stats.bridges_written = edges
    return stats
