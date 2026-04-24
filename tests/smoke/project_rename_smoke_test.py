"""Smoke test for ``gpc project rename``.

Seeds a throwaway project with a repo, a self-metrics row, a Graphify-side
projection (project + repo + node) in Neo4j, and a Qdrant point. Runs the
rename and asserts every layer was rewritten, while the old slug still
resolves via the alias table.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
    Distance,
)

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.graph import neo4j_driver
from gpc.project_rename import rename_project
from gpc.registry import connect, ensure_project, register_repo, resolve_project


OLD_SLUG = "gpc_smoke_rename_old"
NEW_SLUG = "gpc_smoke_rename_new"


def _reset_postgres(slug: str) -> None:
    with connect() as conn:
        conn.execute("delete from gpc_projects where slug = %s", (slug,))


def _reset_neo4j(slug: str) -> None:
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
                   OR (n:GPCEntity AND n.project_slug = $slug)
                DETACH DELETE n
                """,
                slug=slug,
            )


def _reset_qdrant(slug: str, client: QdrantClient) -> None:
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=slug))]
        ),
    )


def _seed_qdrant(slug: str, client: QdrantClient) -> str:
    dim = client.get_collection(COLLECTION_NAME).config.params.vectors.size
    point_id = "00000000-0000-0000-0000-000000000777"
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=[0.0] * dim,
                payload={
                    "project_slug": slug,
                    "project_name": "Smoke Rename",
                    "source_type": "project_file",
                },
            )
        ],
    )
    return point_id


def _seed_neo4j(slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MERGE (p:GraphifyProject {slug: $slug})
                SET p.updated_at = datetime(), p.source = 'gpc'
                MERGE (r:GraphifyRepo {id: $repo_id})
                SET r.slug = 'main', r.project_slug = $slug,
                    r.source = 'graphify', r.updated_at = datetime()
                MERGE (p)-[:HAS_REPO]->(r)
                MERGE (n:GraphifyNode {id: $node_id})
                SET n.project_slug = $slug, n.repo_slug = 'main',
                    n.repo_id = $repo_id, n.source_id = 'seed',
                    n.label = 'seed()', n.norm_label = 'seed()',
                    n.file_type = 'code', n.source_file = 'seed.js'
                MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                """,
                slug=slug,
                repo_id=f"{slug}:main",
                node_id=f"{slug}:main:seed",
            )


@contextmanager
def _isolated():
    _reset_postgres(OLD_SLUG)
    _reset_postgres(NEW_SLUG)
    _reset_neo4j(OLD_SLUG)
    _reset_neo4j(NEW_SLUG)
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    _reset_qdrant(OLD_SLUG, client)
    _reset_qdrant(NEW_SLUG, client)
    try:
        yield client
    finally:
        _reset_postgres(OLD_SLUG)
        _reset_postgres(NEW_SLUG)
        _reset_neo4j(OLD_SLUG)
        _reset_neo4j(NEW_SLUG)
        _reset_qdrant(OLD_SLUG, client)
        _reset_qdrant(NEW_SLUG, client)


def main() -> None:
    with _isolated() as qdrant:
        project = ensure_project(slug=OLD_SLUG, name="Old Smoke Rename")
        # Default repo (same slug as the project) — should be renamed too.
        with connect() as conn:
            conn.execute(
                """
                insert into gpc_repos (project_id, slug, name, root_path)
                values (%s, %s, %s, %s)
                """,
                (
                    project["id"],
                    OLD_SLUG,
                    "default",
                    f"/tmp/{OLD_SLUG}-default",
                ),
            )
            # Second repo with a different slug — must be left alone.
            conn.execute(
                """
                insert into gpc_repos (project_id, slug, name, root_path)
                values (%s, %s, %s, %s)
                """,
                (
                    project["id"],
                    "other-repo",
                    "other",
                    f"/tmp/{OLD_SLUG}-other",
                ),
            )
            # self_metrics row.
            conn.execute(
                """
                insert into gpc_self_metrics (project_id, project_slug, source)
                values (%s, %s, 'snapshot')
                """,
                (project["id"], OLD_SLUG),
            )
        point_id = _seed_qdrant(OLD_SLUG, qdrant)
        _seed_neo4j(OLD_SLUG)

        stats = rename_project(OLD_SLUG, NEW_SLUG, new_name="New Name Smoke")
        assert stats.project_updated, stats
        assert stats.project_name == "New Name Smoke", stats
        assert stats.root_path_updated, stats
        assert OLD_SLUG in stats.aliases_added or NEW_SLUG in stats.aliases_added, stats
        assert len(stats.repos_renamed) == 1, stats
        assert stats.repos_renamed[0]["old"] == OLD_SLUG, stats
        assert stats.self_metrics_rows == 1, stats
        assert stats.qdrant_points_updated == 1, stats
        assert stats.neo4j_nodes_updated >= 3, stats  # project + repo + node

        # Postgres — new slug wins, old slug resolves as alias.
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            refreshed = conn.execute(
                "select slug, name, root_path from gpc_projects where id = %s",
                (project["id"],),
            ).fetchone()
            assert refreshed["slug"] == NEW_SLUG, refreshed
            assert refreshed["name"] == "New Name Smoke", refreshed
            assert refreshed["root_path"].endswith(NEW_SLUG), refreshed

            repos = conn.execute(
                "select slug from gpc_repos where project_id = %s order by slug",
                (project["id"],),
            ).fetchall()
            assert {r["slug"] for r in repos} == {NEW_SLUG, "other-repo"}, repos

            metric = conn.execute(
                "select project_slug from gpc_self_metrics where project_id = %s",
                (project["id"],),
            ).fetchone()
            assert metric["project_slug"] == NEW_SLUG, metric

        resolved_via_alias = resolve_project(project=OLD_SLUG)
        assert resolved_via_alias["slug"] == NEW_SLUG, resolved_via_alias
        resolved_new = resolve_project(project=NEW_SLUG)
        assert resolved_new["slug"] == NEW_SLUG, resolved_new

        # Qdrant — the point now carries the new slug.
        pts = qdrant.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
        assert pts, "seeded point vanished"
        assert pts[0].payload.get("project_slug") == NEW_SLUG, pts[0].payload
        assert pts[0].payload.get("project_name") == "New Name Smoke", pts[0].payload

        # Neo4j — every slug-bearing label was rewritten; no leftover old nodes.
        with neo4j_driver() as driver:
            with driver.session() as session:
                left = session.run(
                    """
                    MATCH (n)
                    WHERE (n:GraphifyProject AND n.slug = $old)
                       OR (n:GraphifyRepo AND n.project_slug = $old)
                       OR (n:GraphifyNode AND n.project_slug = $old)
                       OR (n:GPCProject AND n.slug = $old)
                       OR (n:GPCRepo AND n.project_slug = $old)
                    RETURN count(n) AS c
                    """,
                    old=OLD_SLUG,
                ).single()
                assert left["c"] == 0, left

                new_nodes = session.run(
                    """
                    MATCH (n:GraphifyNode {project_slug: $new})
                    RETURN n.id AS id, n.repo_id AS repo_id
                    """,
                    new=NEW_SLUG,
                ).data()
                assert new_nodes, new_nodes
                for row in new_nodes:
                    assert row["id"].startswith(f"{NEW_SLUG}:"), row
                    assert row["repo_id"].startswith(f"{NEW_SLUG}:"), row

    print("project_rename_smoke_test=passed")


if __name__ == "__main__":
    main()
