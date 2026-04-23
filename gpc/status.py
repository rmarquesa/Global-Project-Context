from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.registry import resolve_project


def get_index_status(
    *,
    project: str | None = None,
    cwd: str | None = None,
    runs: int = 3,
) -> dict[str, Any]:
    resolved_project = resolve_project(project=project, cwd=cwd)
    run_limit = max(1, min(runs, 20))

    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        file_count = conn.execute(
            "select count(*) as count from gpc_files where project_id = %s",
            (resolved_project["id"],),
        ).fetchone()["count"]
        chunk_count = conn.execute(
            "select count(*) as count from gpc_chunks where project_id = %s",
            (resolved_project["id"],),
        ).fetchone()["count"]
        recent_runs = conn.execute(
            """
            select
                id::text as id,
                status,
                started_at,
                finished_at,
                files_seen,
                files_indexed,
                chunks_written,
                error_message,
                metadata
            from gpc_index_runs
            where project_id = %s
            order by started_at desc
            limit %s
            """,
            (resolved_project["id"], run_limit),
        ).fetchall()

    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    point_count = qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(
            must=[
                FieldCondition(
                    key="project_id",
                    match=MatchValue(value=str(resolved_project["id"])),
                )
            ]
        ),
        exact=True,
    ).count

    return {
        "project": resolved_project,
        "files": file_count,
        "chunks": chunk_count,
        "qdrant_collection": COLLECTION_NAME,
        "qdrant_points": point_count,
        "runs": recent_runs,
    }
