"""Atomic delete for a GPC project across Postgres, Qdrant, Neo4j and disk.

Apaga um projeto de ponta a ponta: linhas em Postgres (cascade via FK),
pontos em Qdrant (filtro por ``project_slug``), nós em Neo4j em todos os
labels que carregam o slug, e opcionalmente os artefatos locais (``.gpc/``,
``.gpc.yaml``, hooks git gerenciados pelo GPC).

A remoção dos hooks é **importante** — sem ela, o próximo commit num repo
do projeto ressuscita o projeto no Neo4j via ``graphify post-commit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.registry import normalize_slug


MANAGED_HOOK_MARKER = "GPC managed hook"
HOOK_NAMES = ("post-commit", "post-merge", "post-checkout")


@dataclass
class DeleteStats:
    slug: str
    project_id: str | None = None
    project_deleted: bool = False
    files_deleted: int = 0
    chunks_deleted: int = 0
    entities_deleted: int = 0
    relations_deleted: int = 0
    repos_deleted: int = 0
    aliases_deleted: int = 0
    self_metrics_deleted: int = 0
    qdrant_points_deleted: int = 0
    neo4j_nodes_deleted: int = 0
    neo4j_relationships_deleted: int = 0
    hooks_removed: list[str] = field(default_factory=list)
    hooks_skipped: list[str] = field(default_factory=list)
    local_files_removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ProjectDeleteError(RuntimeError):
    pass


def delete_project(
    slug: str,
    *,
    remove_hooks: bool = True,
    remove_local_files: bool = True,
) -> DeleteStats:
    """Delete ``slug`` end-to-end.

    Order of operations:
        1. Collect filesystem roots **before** Postgres delete (the rows we
           need for the paths disappear after the cascade).
        2. Count + delete in Postgres (transaction — cascade handles FK
           children).
        3. Delete Qdrant points filtered by ``project_slug``.
        4. Delete every Neo4j node that carries the slug (any label) plus
           the relationships stamped with that ``project_slug``.
        5. Remove GPC-managed hooks and ``.gpc/`` / ``.gpc.yaml`` from each
           collected filesystem root, when ``remove_hooks`` /
           ``remove_local_files`` allow it.

    Qdrant and Neo4j failures get captured in ``stats.warnings`` and do
    not roll back the Postgres deletion — the operation is idempotent
    enough that a second call finishes the job.
    """

    slug = normalize_slug(slug)
    stats = DeleteStats(slug=slug)

    filesystem_roots = _collect_filesystem_roots(slug, stats)
    if stats.project_id is None:
        raise ProjectDeleteError(f"Project {slug!r} not found")

    _delete_postgres(slug, stats)

    try:
        _delete_qdrant(slug, stats)
    except Exception as exc:  # noqa: BLE001
        stats.warnings.append(f"qdrant: {exc}")

    try:
        _delete_neo4j(slug, stats)
    except Exception as exc:  # noqa: BLE001
        stats.warnings.append(f"neo4j: {exc}")

    for root in filesystem_roots:
        if remove_hooks:
            _remove_hooks(root, stats)
        if remove_local_files:
            _remove_local_files(root, stats)

    return stats


def _collect_filesystem_roots(slug: str, stats: DeleteStats) -> list[Path]:
    """Return every real (non-virtual) root associated with the project."""

    roots: list[Path] = []
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        project = conn.execute(
            "select id::text as id, root_path from gpc_projects where slug = %s",
            (slug,),
        ).fetchone()
        if not project:
            return roots
        stats.project_id = project["id"]
        if project["root_path"] and not str(project["root_path"]).startswith("virtual://"):
            roots.append(Path(project["root_path"]))
        repos = conn.execute(
            "select root_path from gpc_repos where project_id = %s",
            (project["id"],),
        ).fetchall()
        for repo in repos:
            if repo["root_path"]:
                roots.append(Path(repo["root_path"]))

    # Dedupe by canonical path so symlinks like /var → /private/var on macOS
    # don't cause us to iterate the same directory twice.
    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        try:
            canonical = root.resolve(strict=False)
        except OSError:
            canonical = root
        key = str(canonical)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(canonical)
    return ordered


def _delete_postgres(slug: str, stats: DeleteStats) -> None:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        with conn.transaction():
            pid = stats.project_id
            counts = conn.execute(
                """
                select
                    (select count(*) from gpc_files where project_id = %s) as files,
                    (select count(*) from gpc_chunks where project_id = %s) as chunks,
                    (select count(*) from gpc_entities where project_id = %s) as entities,
                    (select count(*) from gpc_relations where project_id = %s) as relations,
                    (select count(*) from gpc_repos where project_id = %s) as repos,
                    (select count(*) from gpc_project_aliases where project_id = %s) as aliases,
                    (select count(*) from gpc_self_metrics where project_id = %s) as metrics
                """,
                (pid, pid, pid, pid, pid, pid, pid),
            ).fetchone()
            stats.files_deleted = int(counts["files"] or 0)
            stats.chunks_deleted = int(counts["chunks"] or 0)
            stats.entities_deleted = int(counts["entities"] or 0)
            stats.relations_deleted = int(counts["relations"] or 0)
            stats.repos_deleted = int(counts["repos"] or 0)
            stats.aliases_deleted = int(counts["aliases"] or 0)
            stats.self_metrics_deleted = int(counts["metrics"] or 0)

            conn.execute("delete from gpc_projects where id = %s", (pid,))
            stats.project_deleted = True


def _delete_qdrant(slug: str, stats: DeleteStats) -> None:
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    count = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=slug))]
        ),
        exact=True,
    ).count
    stats.qdrant_points_deleted = int(count or 0)
    if stats.qdrant_points_deleted == 0:
        return
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="project_slug", match=MatchValue(value=slug))]
        ),
    )


def _delete_neo4j(slug: str, stats: DeleteStats) -> None:
    from gpc.graph import neo4j_driver

    with neo4j_driver() as driver:
        with driver.session() as session:
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
                slug=slug,
            ).single()
            if result:
                stats.neo4j_nodes_deleted = int(result["nodes_deleted"] or 0)
                stats.neo4j_relationships_deleted = int(result["rels_deleted"] or 0)


def _remove_hooks(root: Path, stats: DeleteStats) -> None:
    """Remove only hooks tagged with ``MANAGED_HOOK_MARKER``. Custom hooks
    are left in place and reported under ``hooks_skipped`` so the operator
    knows the hook is still there."""

    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.exists():
        return
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if not hook.exists():
            continue
        try:
            text = hook.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            stats.warnings.append(f"read hook {hook}: {exc}")
            continue
        if MANAGED_HOOK_MARKER in text:
            try:
                hook.unlink()
                stats.hooks_removed.append(str(hook))
            except OSError as exc:
                stats.warnings.append(f"unlink hook {hook}: {exc}")
        else:
            stats.hooks_skipped.append(str(hook))


def _remove_local_files(root: Path, stats: DeleteStats) -> None:
    """Remove ``.gpc/`` and ``.gpc.yaml`` from a project root."""

    import shutil as _shutil

    for rel in (".gpc.yaml", ".gpc"):
        target = root / rel
        if not target.exists():
            continue
        try:
            if target.is_dir():
                _shutil.rmtree(target)
            else:
                target.unlink()
            stats.local_files_removed.append(str(target))
        except OSError as exc:
            stats.warnings.append(f"remove {target}: {exc}")
