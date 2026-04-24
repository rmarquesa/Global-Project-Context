"""Atomic rename for a GPC project across Postgres, Qdrant, and Neo4j.

A project's identity lives in three places:

* Postgres — ``gpc_projects.slug``, plus every row whose foreign key is the
  project id (files, chunks, entities, relations, repos, aliases); plus
  ``gpc_self_metrics.project_slug`` which is denormalised.
* Qdrant — ``project_slug`` / ``project_name`` / ``root_path`` stamped on
  every point payload.
* Neo4j — ``slug`` / ``project_slug`` on ``GPCProject`` / ``GPCRepo`` /
  ``GPCEntity`` / ``GraphifyProject`` / ``GraphifyRepo`` / ``GraphifyNode``,
  plus the ``id`` strings we mint for Graphify nodes which embed the slug.

``rename_project`` walks the three layers in one shot and returns stats so
callers can inspect what was touched. Postgres is updated in a single
transaction; Qdrant and Neo4j updates are idempotent enough that re-running
is safe if one leg fails halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.registry import normalize_slug


@dataclass
class RenameStats:
    old_slug: str
    new_slug: str
    project_id: str | None = None
    project_name: str | None = None
    project_updated: bool = False
    root_path_updated: bool = False
    aliases_added: list[str] = field(default_factory=list)
    repos_renamed: list[dict[str, str]] = field(default_factory=list)
    self_metrics_rows: int = 0
    qdrant_points_updated: int = 0
    neo4j_nodes_updated: int = 0
    neo4j_edges_updated: int = 0
    warnings: list[str] = field(default_factory=list)


class ProjectRenameError(RuntimeError):
    pass


def rename_project(
    old_slug: str,
    new_slug: str,
    *,
    new_name: str | None = None,
    rename_default_repo: bool = True,
) -> RenameStats:
    """Rename a project end-to-end.

    Args:
        old_slug: current slug of the project.
        new_slug: target slug. Must pass :func:`normalize_slug` validation
            and must not collide with another existing project.
        new_name: optional new display name; unchanged if ``None``.
        rename_default_repo: when the project owns a repo whose slug matches
            ``old_slug`` (the default repo created by ``gpc init`` without
            ``--repo``), also rename that repo to ``new_slug``. Other repos
            are left alone.

    Returns:
        :class:`RenameStats` summarising what was changed. Callers can log
        or surface this to CLI output.

    Raises:
        ProjectRenameError: if the old project does not exist, the new
            slug is already taken by a different project, or the slugs are
            equal.
    """

    old_slug = normalize_slug(old_slug)
    new_slug = normalize_slug(new_slug)
    if old_slug == new_slug and not new_name:
        raise ProjectRenameError("new slug is identical to old slug — nothing to do")

    stats = RenameStats(old_slug=old_slug, new_slug=new_slug, project_name=new_name)

    _rename_postgres(stats, new_name=new_name, rename_default_repo=rename_default_repo)
    try:
        _rename_qdrant(stats)
    except Exception as exc:  # noqa: BLE001 — carry on to Neo4j
        stats.warnings.append(f"qdrant: {exc}")
    try:
        _rename_neo4j(stats)
    except Exception as exc:  # noqa: BLE001
        stats.warnings.append(f"neo4j: {exc}")

    return stats


def _rename_postgres(
    stats: RenameStats,
    *,
    new_name: str | None,
    rename_default_repo: bool,
) -> None:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        with conn.transaction():
            project = conn.execute(
                "select id, slug, name, root_path from gpc_projects where slug = %s",
                (stats.old_slug,),
            ).fetchone()
            if not project:
                raise ProjectRenameError(f"Project {stats.old_slug!r} not found")

            # Block rename into an existing, different project.
            if stats.old_slug != stats.new_slug:
                collision = conn.execute(
                    "select id from gpc_projects where slug = %s and id <> %s",
                    (stats.new_slug, project["id"]),
                ).fetchone()
                if collision:
                    raise ProjectRenameError(
                        f"Cannot rename: slug {stats.new_slug!r} already used by "
                        f"another project (id={collision['id']})"
                    )

            stats.project_id = str(project["id"])
            target_name = new_name or project["name"]
            stats.project_name = target_name

            # Update the project row. If the root_path is the virtual
            # placeholder we mint in ensure_project, also rewrite it so
            # the pointer stays in sync with the new slug.
            virtual_root = f"virtual://gpc/projects/{stats.old_slug}"
            new_virtual_root = f"virtual://gpc/projects/{stats.new_slug}"
            new_root_path = (
                new_virtual_root if project["root_path"] == virtual_root else project["root_path"]
            )
            conn.execute(
                """
                update gpc_projects
                set slug = %s, name = %s, root_path = %s
                where id = %s
                """,
                (stats.new_slug, target_name, new_root_path, project["id"]),
            )
            stats.project_updated = True
            stats.root_path_updated = new_root_path != project["root_path"]

            # Keep the old slug as an alias so cross-session references
            # (``.gpc.yaml``, prior chat links, external scripts) still work.
            # Add the new slug as an alias too; UNIQUE(alias) is global so
            # we skip rather than fail on conflicts.
            for alias in (stats.old_slug, stats.new_slug):
                inserted = conn.execute(
                    """
                    insert into gpc_project_aliases (project_id, alias, alias_type)
                    values (%s, %s, 'rename')
                    on conflict (alias) do nothing
                    returning alias
                    """,
                    (project["id"], alias),
                ).fetchone()
                if inserted:
                    stats.aliases_added.append(inserted["alias"])

            if rename_default_repo:
                repos = conn.execute(
                    "select id, slug from gpc_repos where project_id = %s",
                    (project["id"],),
                ).fetchall()
                for repo in repos:
                    if repo["slug"] != stats.old_slug:
                        continue
                    # Do not collide with an existing repo that already has new_slug.
                    existing = conn.execute(
                        """
                        select id from gpc_repos
                        where project_id = %s and slug = %s and id <> %s
                        """,
                        (project["id"], stats.new_slug, repo["id"]),
                    ).fetchone()
                    if existing:
                        stats.warnings.append(
                            f"skipped repo rename: another repo already uses {stats.new_slug!r}"
                        )
                        continue
                    conn.execute(
                        "update gpc_repos set slug = %s where id = %s",
                        (stats.new_slug, repo["id"]),
                    )
                    stats.repos_renamed.append(
                        {"old": repo["slug"], "new": stats.new_slug, "id": str(repo["id"])}
                    )

            # Denormalised slug column on gpc_self_metrics.
            updated = conn.execute(
                """
                update gpc_self_metrics
                set project_slug = %s
                where project_id = %s and project_slug = %s
                """,
                (stats.new_slug, project["id"], stats.old_slug),
            ).rowcount or 0
            stats.self_metrics_rows = int(updated)


def _rename_qdrant(stats: RenameStats) -> None:
    if not stats.project_updated:
        return
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # Count first so the stats reflect how many points were rewritten.
    count = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=stats.old_slug))]
        ),
        exact=True,
    ).count
    stats.qdrant_points_updated = int(count or 0)
    if stats.qdrant_points_updated == 0:
        return

    client.set_payload(
        collection_name=COLLECTION_NAME,
        payload={
            "project_slug": stats.new_slug,
            "project_name": stats.project_name,
        },
        points=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=stats.old_slug))]
        ),
        wait=True,
    )


def _rename_neo4j(stats: RenameStats) -> None:
    from gpc.graph import neo4j_driver

    if not stats.project_updated:
        return

    with neo4j_driver() as driver:
        with driver.session() as session:
            # Rename every slug-bearing property on the relevant labels.
            props_by_label = {
                "GPCProject": "slug",
                "GraphifyProject": "slug",
                "GPCRepo": "project_slug",
                "GPCEntity": "project_slug",
                "GraphifyRepo": "project_slug",
                "GraphifyNode": "project_slug",
            }
            total_nodes = 0
            for label, prop in props_by_label.items():
                record = session.run(
                    f"""
                    MATCH (n:{label} {{{prop}: $old}})
                    SET n.{prop} = $new
                    RETURN count(n) AS c
                    """,
                    old=stats.old_slug, new=stats.new_slug,
                ).single()
                total_nodes += int(record["c"] or 0) if record else 0
            stats.neo4j_nodes_updated = total_nodes

            # Graphify node ids embed the old slug at the start: rewrite
            # ``old_slug:repo:source_id`` → ``new_slug:repo:source_id``. Same
            # for ``repo_id`` and ``repo_slug`` on GraphifyRepo.
            session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $new})
                WHERE n.id STARTS WITH $old_prefix
                SET n.id = $new_prefix + substring(n.id, size($old_prefix))
                """,
                new=stats.new_slug,
                old_prefix=f"{stats.old_slug}:",
                new_prefix=f"{stats.new_slug}:",
            )
            session.run(
                """
                MATCH (r:GraphifyRepo {project_slug: $new})
                WHERE r.id STARTS WITH $old_prefix
                SET r.id = $new_prefix + substring(r.id, size($old_prefix)),
                    r.repo_id = coalesce($new_prefix + substring(r.id, size($old_prefix)), r.repo_id)
                """,
                new=stats.new_slug,
                old_prefix=f"{stats.old_slug}:",
                new_prefix=f"{stats.new_slug}:",
            )
            session.run(
                """
                MATCH (n:GraphifyNode {project_slug: $new})
                WHERE n.repo_id IS NOT NULL AND n.repo_id STARTS WITH $old_prefix
                SET n.repo_id = $new_prefix + substring(n.repo_id, size($old_prefix))
                """,
                new=stats.new_slug,
                old_prefix=f"{stats.old_slug}:",
                new_prefix=f"{stats.new_slug}:",
            )

            # Rewrite CROSS_REPO_BRIDGE edges scoped to the project.
            rels = session.run(
                """
                MATCH (:GraphifyNode {project_slug: $new})
                      -[r:CROSS_REPO_BRIDGE]->
                      (:GraphifyNode)
                WHERE r.project_slug = $old
                SET r.project_slug = $new
                RETURN count(r) AS c
                """,
                old=stats.old_slug, new=stats.new_slug,
            ).single()
            stats.neo4j_edges_updated = int(rels["c"] or 0) if rels else 0

            # GRAPHIFY_RELATION edges also carry a project_slug in their
            # payload — keep them aligned.
            more = session.run(
                """
                MATCH (:GraphifyNode {project_slug: $new})
                      -[r:GRAPHIFY_RELATION]->
                      (:GraphifyNode)
                WHERE r.project_slug = $old
                SET r.project_slug = $new
                RETURN count(r) AS c
                """,
                old=stats.old_slug, new=stats.new_slug,
            ).single()
            if more:
                stats.neo4j_edges_updated += int(more["c"] or 0)
