"""Smoke test for ``gpc.graph_reset`` scoped to a synthetic project slug.

Creates throwaway Graphify + GPC nodes tagged with an isolated project slug,
runs ``reset_neo4j`` with that slug, and asserts only the tagged nodes got
deleted. Never touches production slugs.
"""

from __future__ import annotations

from contextlib import contextmanager

from gpc.graph import neo4j_driver
from gpc.graph_reset import reset_neo4j


TEST_PROJECT_SLUG = "gpc_smoke_reset"
NEIGHBOUR_PROJECT_SLUG = "gpc_smoke_reset_other"


def _seed(slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                "MERGE (p:GraphifyProject {slug: $slug}) SET p.source='gpc', p.updated_at=datetime()",
                slug=slug,
            )
            session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})
                MERGE (r:GraphifyRepo {id: $repo_id})
                SET r.slug = 'repo_a', r.project_slug = $slug,
                    r.source = 'graphify', r.updated_at = datetime()
                MERGE (p)-[:HAS_REPO]->(r)
                MERGE (n:GraphifyNode {id: $node_id})
                SET n.project_slug = $slug, n.repo_slug = 'repo_a',
                    n.label = 'seed()', n.norm_label='seed()',
                    n.file_type='code', n.source_file='seed.js',
                    n.source='graphify', n.updated_at=datetime()
                MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                """,
                slug=slug,
                repo_id=f"{slug}:repo_a",
                node_id=f"{slug}:repo_a:seed",
            )
            session.run(
                """
                MERGE (gp:GPCProject {id: $gp_id})
                SET gp.slug = $slug, gp.updated_at = datetime()
                MERGE (gr:GPCRepo {id: $gr_id})
                SET gr.slug='repo_a', gr.project_slug=$slug, gr.project_id=$gp_id, gr.updated_at=datetime()
                MERGE (gp)-[:OWNS_REPO]->(gr)
                """,
                gp_id=f"{slug}-gp",
                gr_id=f"{slug}-gr",
                slug=slug,
            )


def _count(slug: str) -> int:
    with neo4j_driver() as driver:
        with driver.session() as session:
            return (
                session.run(
                    """
                    MATCH (n)
                    WHERE (n:GraphifyProject AND n.slug = $slug)
                       OR (n:GraphifyRepo AND n.project_slug = $slug)
                       OR (n:GraphifyNode AND n.project_slug = $slug)
                       OR (n:GPCProject AND n.slug = $slug)
                       OR (n:GPCRepo AND n.project_slug = $slug)
                    RETURN count(n) AS c
                    """,
                    slug=slug,
                ).single()["c"]
            )


def _cleanup(slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (n)
                WHERE (n:GraphifyProject AND n.slug = $slug)
                   OR (n:GraphifyRepo AND n.project_slug = $slug)
                   OR (n:GraphifyNode AND n.project_slug = $slug)
                   OR (n:GPCProject AND n.slug = $slug)
                   OR (n:GPCRepo AND n.project_slug = $slug)
                DETACH DELETE n
                """,
                slug=slug,
            )


@contextmanager
def _isolated():
    _cleanup(TEST_PROJECT_SLUG)
    _cleanup(NEIGHBOUR_PROJECT_SLUG)
    try:
        _seed(TEST_PROJECT_SLUG)
        _seed(NEIGHBOUR_PROJECT_SLUG)
        yield
    finally:
        _cleanup(TEST_PROJECT_SLUG)
        _cleanup(NEIGHBOUR_PROJECT_SLUG)


def main() -> None:
    with _isolated():
        assert _count(TEST_PROJECT_SLUG) > 0, "seed did not create test nodes"
        assert _count(NEIGHBOUR_PROJECT_SLUG) > 0, "seed did not create neighbour nodes"

        # Scope to TEST_PROJECT_SLUG only — neighbour must survive.
        stats = reset_neo4j(project_slug=TEST_PROJECT_SLUG)
        assert stats.neo4j_nodes_deleted >= 1, stats
        assert _count(TEST_PROJECT_SLUG) == 0, "test nodes must be wiped"
        assert _count(NEIGHBOUR_PROJECT_SLUG) > 0, "neighbour must not be affected"

    print("graph_reset_smoke_test=passed")


if __name__ == "__main__":
    main()
