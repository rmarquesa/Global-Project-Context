"""Nuclear reset helpers.

``reset_all()`` rebuilds Postgres + Neo4j + Qdrant from scratch. Intended for
greenfield onboarding of the new project+repos model, and for any scenario
where the state drifted beyond repair.

Never call this from a hook. Never call it without explicit user confirmation.
The CLI entrypoint (``gpc reset``) requires ``--yes`` and prints an explicit
audit trail before wiping anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg

from gpc.config import POSTGRES_DSN, ROOT_DIR
from gpc.graph import neo4j_driver
from gpc.graph_reset import reset_neo4j


@dataclass
class ResetReport:
    postgres_tables_dropped: list[str] = field(default_factory=list)
    postgres_migrations_applied: list[str] = field(default_factory=list)
    neo4j_nodes_deleted: int = 0
    neo4j_relationships_deleted: int = 0
    qdrant_collection_recreated: bool = False


GPC_TABLES = (
    # Order matters — child tables first so FKs don't block drops.
    "gpc_project_sources",
    "gpc_sources",
    "gpc_project_aliases",
    "gpc_graph_projections",
    "gpc_index_runs",
    "gpc_events",
    "gpc_decisions",
    "gpc_relations",
    "gpc_entities",
    "gpc_chunks",
    "gpc_files",
    "gpc_repos",
    "gpc_projects",
    "gpc_schema_migrations",
)


def drop_postgres_schema() -> list[str]:
    """Drop every ``gpc_*`` table. Returns the tables that were dropped."""

    dropped: list[str] = []
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute("set client_min_messages to warning")
        with conn.cursor() as cur:
            for table in GPC_TABLES:
                cur.execute(f"drop table if exists {table} cascade")
                if cur.statusmessage and cur.statusmessage.startswith("DROP TABLE"):
                    dropped.append(table)
            # Drop the touch trigger function if nothing else depends on it.
            cur.execute("drop function if exists gpc_touch_updated_at() cascade")
        conn.commit()
    return dropped


def apply_migrations(run_module: Callable[[str, list[str]], int]) -> list[str]:
    """Invoke ``scripts.migrate up`` and capture the applied versions."""

    before = _applied_versions()
    rc = run_module("scripts.migrate", ["up"])
    if rc != 0:
        raise RuntimeError(f"migrations up exited with code {rc}")
    after = _applied_versions()
    return sorted(set(after) - set(before))


def _applied_versions() -> list[str]:
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            rows = conn.execute(
                "select version from gpc_schema_migrations order by version"
            ).fetchall()
        return [row[0] for row in rows]
    except psycopg.errors.UndefinedTable:
        return []
    except Exception:
        return []


def reset_qdrant(run_module: Callable[[str, list[str]], int]) -> bool:
    """Drop and recreate the Qdrant collection via ``scripts.init_qdrant --reset``."""

    rc = run_module("scripts.init_qdrant", ["--reset"])
    if rc != 0:
        raise RuntimeError(f"init_qdrant --reset exited with code {rc}")
    return True


def reset_all(
    *,
    run_module: Callable[[str, list[str]], int],
    postgres: bool = True,
    neo4j: bool = True,
    qdrant: bool = True,
) -> ResetReport:
    """Nuclear reset across Postgres, Neo4j, and Qdrant.

    Each backend can be disabled individually to keep the command testable.
    The default is "everything". Postgres is dropped before migrations so the
    next run starts from a clean schema that includes ``gpc_repos``.
    """

    report = ResetReport()

    if neo4j:
        neo = reset_neo4j()
        report.neo4j_nodes_deleted = neo.neo4j_nodes_deleted
        report.neo4j_relationships_deleted = neo.neo4j_relationships_deleted

    if postgres:
        report.postgres_tables_dropped = drop_postgres_schema()
        report.postgres_migrations_applied = apply_migrations(run_module)

    if qdrant:
        report.qdrant_collection_recreated = reset_qdrant(run_module)

    return report
