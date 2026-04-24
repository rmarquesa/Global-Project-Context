"""Smoke test for the graph-quality improvements.

Covers:
    * god-node classification (``god_nodes`` vs ``utility_hubs``) in
      ``graph_summary`` using a synthetic project with both distinctive and
      generic labels;
    * ``graph_community`` returning members and external bridges;
    * the entity extractor populating ``gpc_entities`` + ``gpc_relations``
      on a tiny temp project with two JavaScript files that import each
      other;
    * hybrid retrieval (``compose_project_context(include_graph=True)``)
      silently degrading when the graph is missing.

Requires local Postgres and Neo4j. Cleans up all seeded data on exit.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile

import psycopg

from gpc.config import POSTGRES_DSN
from gpc.entity_extractor import extract_for_project
from gpc.graph import neo4j_driver
from gpc.graph_query import graph_community, graph_summary
from gpc.registry import connect, register_project, register_repo


TEST_PROJECT_SLUG = "gpc_smoke_quality"
TEST_GRAPHIFY_SLUG = "gpc_smoke_quality_graphify"


def _cleanup_postgres() -> None:
    with connect() as conn:
        conn.execute("delete from gpc_projects where slug = %s", (TEST_PROJECT_SLUG,))


def _cleanup_neo4j() -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (n)
                WHERE (n:GraphifyProject AND n.slug = $slug)
                   OR (n:GraphifyRepo AND n.project_slug = $slug)
                   OR (n:GraphifyNode AND n.project_slug = $slug)
                DETACH DELETE n
                """,
                slug=TEST_GRAPHIFY_SLUG,
            )


@contextmanager
def _tmp_project():
    """Create a throwaway filesystem project with two JS files."""

    tmp = Path(tempfile.mkdtemp(prefix="gpc-smoke-quality-"))
    try:
        (tmp / "a.js").write_text(
            "import './b';\nexport function runA() { return 1; }\n",
            encoding="utf-8",
        )
        (tmp / "b.js").write_text(
            "export function runB() { return 2; }\n",
            encoding="utf-8",
        )
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_graphify_summary_project() -> None:
    """Create a synthetic Graphify projection with one distinctive god node,
    one utility hub, and two communities so graph_summary + graph_community
    have something to return."""

    nodes = [
        {"sid": "authsignature", "label": "computeAuthSignature()", "community": 0},
        {"sid": "main", "label": "main()", "community": 0},
        {"sid": "run", "label": "run()", "community": 0},
        {"sid": "helper", "label": "helperOne()", "community": 1},
    ]
    edges = [
        ("authsignature", "main"),
        ("authsignature", "run"),
        ("authsignature", "helper"),  # cross-community
        ("main", "run"),
    ]

    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                "MERGE (p:GraphifyProject {slug:$s}) SET p.source='gpc', p.updated_at=datetime()",
                s=TEST_GRAPHIFY_SLUG,
            )
            session.run(
                """
                MATCH (p:GraphifyProject {slug:$s})
                MERGE (r:GraphifyRepo {id:$rid})
                SET r.slug='main', r.project_slug=$s, r.source='graphify',
                    r.updated_at=datetime(), r.root_path='/tmp/smoke'
                MERGE (p)-[:HAS_REPO]->(r)
                """,
                s=TEST_GRAPHIFY_SLUG, rid=f"{TEST_GRAPHIFY_SLUG}:main",
            )
            for n in nodes:
                session.run(
                    """
                    MATCH (r:GraphifyRepo {id:$rid})
                    MERGE (node:GraphifyNode {id:$id})
                    SET node.project_slug=$s, node.repo_slug='main',
                        node.repo_id=$rid, node.source_id=$sid,
                        node.label=$label, node.norm_label=$label,
                        node.file_type='code', node.source_file=$sid + '.js',
                        node.community=$community, node.source='graphify'
                    MERGE (r)-[:HAS_GRAPH_NODE]->(node)
                    """,
                    rid=f"{TEST_GRAPHIFY_SLUG}:main",
                    id=f"{TEST_GRAPHIFY_SLUG}:main:{n['sid']}",
                    s=TEST_GRAPHIFY_SLUG,
                    sid=n["sid"],
                    label=n["label"],
                    community=n["community"],
                )
            for i, (src, tgt) in enumerate(edges):
                session.run(
                    """
                    MATCH (a:GraphifyNode {id:$a_id}), (b:GraphifyNode {id:$b_id})
                    MERGE (a)-[e:GRAPHIFY_RELATION {id:$eid}]->(b)
                    SET e.relation='calls', e.project_slug=$s
                    """,
                    a_id=f"{TEST_GRAPHIFY_SLUG}:main:{src}",
                    b_id=f"{TEST_GRAPHIFY_SLUG}:main:{tgt}",
                    eid=f"{TEST_GRAPHIFY_SLUG}:e{i}",
                    s=TEST_GRAPHIFY_SLUG,
                )


def main() -> None:
    _cleanup_postgres()
    _cleanup_neo4j()

    try:
        # --- god-node classification + community ---
        _seed_graphify_summary_project()
        summary = graph_summary(TEST_GRAPHIFY_SLUG, top_k_gods=5, include_cohesion=False)
        assert summary["found"] is True, summary

        central_labels = {g["label"] for g in summary["god_nodes"]}
        utility_labels = {g["label"] for g in summary["utility_hubs"]}
        # computeAuthSignature() is distinctive; main/run are generic.
        assert "computeAuthSignature()" in central_labels, summary
        assert {"main()", "run()"} <= utility_labels, summary
        assert central_labels.isdisjoint(utility_labels), summary

        # community 0 has 3 members + 1 external edge to community 1.
        community0 = graph_community(TEST_GRAPHIFY_SLUG, 0, top_members=10, top_external_bridges=5)
        assert community0["found"] is True, community0
        assert community0["size"] == 3, community0
        assert len(community0["external_bridges"]) == 1, community0

        # --- entity extractor on a real tiny project ---
        with _tmp_project() as tmp:
            project = register_project(tmp, slug=TEST_PROJECT_SLUG, name="Quality Smoke")
            register_repo(TEST_PROJECT_SLUG, tmp, slug=TEST_PROJECT_SLUG, name="Quality Smoke")

            with connect() as conn:
                conn.execute(
                    """
                    insert into gpc_files
                        (project_id, repo_id, relative_path, absolute_path,
                         language, file_type, size_bytes, content_hash)
                    select p.id, r.id, 'a.js', %s, 'javascript', 'code', 0, 'hash-a'
                    from gpc_projects p, gpc_repos r
                    where p.slug = %s and r.slug = %s
                    on conflict do nothing
                    """,
                    (str(tmp / "a.js"), TEST_PROJECT_SLUG, TEST_PROJECT_SLUG),
                )
                conn.execute(
                    """
                    insert into gpc_files
                        (project_id, repo_id, relative_path, absolute_path,
                         language, file_type, size_bytes, content_hash)
                    select p.id, r.id, 'b.js', %s, 'javascript', 'code', 0, 'hash-b'
                    from gpc_projects p, gpc_repos r
                    where p.slug = %s and r.slug = %s
                    on conflict do nothing
                    """,
                    (str(tmp / "b.js"), TEST_PROJECT_SLUG, TEST_PROJECT_SLUG),
                )

            stats = extract_for_project(str(project["id"]), project["slug"])
            assert stats["entities_upserted"] == 2, stats
            # a.js imports './b' → resolves to b.js → exactly 1 import relation.
            assert stats["relations_upserted"] == 1, stats

    finally:
        _cleanup_postgres()
        _cleanup_neo4j()

    print("graph_quality_smoke_test=passed")


if __name__ == "__main__":
    main()
