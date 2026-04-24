"""Smoke test for ``gpc project delete``.

Seeds a throwaway project with rows across every affected table, a Qdrant
point, a Graphify-side Neo4j projection, and a fake filesystem root with
one managed hook + one custom hook + .gpc.yaml + .gpc/ directory. Runs
delete and asserts everything was wiped except the custom hook, which is
reported in ``hooks_skipped``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import tempfile

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
)

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.graph import neo4j_driver
from gpc.project_delete import MANAGED_HOOK_MARKER, ProjectDeleteError, delete_project
from gpc.registry import connect, ensure_project


SLUG = "gpc_smoke_delete"


def _reset_postgres() -> None:
    with connect() as conn:
        conn.execute("delete from gpc_projects where slug = %s", (SLUG,))


def _reset_neo4j() -> None:
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
                slug=SLUG,
            )


def _reset_qdrant(client: QdrantClient) -> None:
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=SLUG))]
        ),
    )


def _seed_neo4j() -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MERGE (p:GraphifyProject {slug: $slug})
                SET p.source = 'gpc', p.updated_at = datetime()
                MERGE (r:GraphifyRepo {id: $repo_id})
                SET r.slug = 'main', r.project_slug = $slug,
                    r.source = 'graphify', r.updated_at = datetime()
                MERGE (p)-[:HAS_REPO]->(r)
                MERGE (n:GraphifyNode {id: $node_id})
                SET n.project_slug = $slug, n.repo_slug = 'main',
                    n.label = 'seed()', n.file_type = 'code',
                    n.source_file = 'seed.js'
                MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                """,
                slug=SLUG,
                repo_id=f"{SLUG}:main",
                node_id=f"{SLUG}:main:seed",
            )


def _seed_qdrant(client: QdrantClient) -> str:
    dim = client.get_collection(COLLECTION_NAME).config.params.vectors.size
    point_id = "00000000-0000-0000-0000-000000000888"
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=[0.0] * dim,
                payload={
                    "project_slug": SLUG,
                    "source_type": "project_file",
                },
            )
        ],
    )
    return point_id


def _seed_filesystem(tmp: Path) -> tuple[Path, Path]:
    """Write a managed hook + custom hook + .gpc/ + .gpc.yaml."""

    hooks = tmp / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    managed = hooks / "post-commit"
    managed.write_text(
        f"#!/bin/sh\n# {MANAGED_HOOK_MARKER}\necho hi\n", encoding="utf-8"
    )
    managed.chmod(managed.stat().st_mode | stat.S_IEXEC)
    custom = hooks / "post-checkout"
    custom.write_text("#!/bin/sh\n# user custom hook\necho custom\n", encoding="utf-8")
    custom.chmod(custom.stat().st_mode | stat.S_IEXEC)

    gpc_dir = tmp / ".gpc"
    gpc_dir.mkdir(exist_ok=True)
    (gpc_dir / "notes.txt").write_text("stuff", encoding="utf-8")
    (tmp / ".gpc.yaml").write_text(f"project: {SLUG}\n", encoding="utf-8")
    return managed, custom


@contextmanager
def _isolated():
    _reset_postgres()
    _reset_neo4j()
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    _reset_qdrant(client)
    tmp = Path(tempfile.mkdtemp(prefix="gpc-delete-smoke-")).resolve()
    try:
        yield client, tmp
    finally:
        _reset_postgres()
        _reset_neo4j()
        _reset_qdrant(client)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    with _isolated() as (qdrant, tmp):
        project = ensure_project(slug=SLUG, name="Delete Smoke", root_path=tmp)

        with connect() as conn:
            conn.execute(
                """
                insert into gpc_repos (project_id, slug, name, root_path)
                values (%s, %s, %s, %s)
                """,
                (project["id"], SLUG, "default", str(tmp)),
            )
            conn.execute(
                """
                insert into gpc_self_metrics (project_id, project_slug, source)
                values (%s, %s, 'snapshot')
                """,
                (project["id"], SLUG),
            )

        point_id = _seed_qdrant(qdrant)
        _seed_neo4j()
        managed_hook, custom_hook = _seed_filesystem(tmp)

        stats = delete_project(SLUG)
        assert stats.project_deleted, stats
        assert stats.repos_deleted == 1, stats
        assert stats.aliases_deleted >= 1, stats
        assert stats.self_metrics_deleted == 1, stats
        assert stats.qdrant_points_deleted == 1, stats
        assert stats.neo4j_nodes_deleted >= 3, stats

        assert str(managed_hook) in stats.hooks_removed, stats
        assert str(custom_hook) in stats.hooks_skipped, stats
        assert not managed_hook.exists(), "managed hook should be gone"
        assert custom_hook.exists(), "custom hook must survive"

        assert any(".gpc.yaml" in path for path in stats.local_files_removed), stats
        assert any(".gpc" in path and ".gpc.yaml" not in path for path in stats.local_files_removed), stats
        assert not (tmp / ".gpc").exists()
        assert not (tmp / ".gpc.yaml").exists()

        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            row = conn.execute(
                "select 1 from gpc_projects where slug = %s", (SLUG,)
            ).fetchone()
            assert row is None, row

        points = qdrant.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
        assert not points, "qdrant point should be gone"

        with neo4j_driver() as driver:
            with driver.session() as session:
                left = session.run(
                    """
                    MATCH (n)
                    WHERE (n:GraphifyProject AND n.slug = $slug)
                       OR (n:GraphifyRepo AND n.project_slug = $slug)
                       OR (n:GraphifyNode AND n.project_slug = $slug)
                    RETURN count(n) AS c
                    """,
                    slug=SLUG,
                ).single()
                assert left["c"] == 0, left

        # Second call raises — project is gone.
        try:
            delete_project(SLUG)
        except ProjectDeleteError:
            pass
        else:
            raise AssertionError("second delete should have raised ProjectDeleteError")

    print("project_delete_smoke_test=passed")


if __name__ == "__main__":
    main()
