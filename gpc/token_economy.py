from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
import tiktoken

from gpc.config import POSTGRES_DSN
from gpc.registry import resolve_project
from gpc.search import compose_project_context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate prompt token savings from using GPC retrieval instead of "
            "sending all indexed project chunks."
        )
    )
    parser.add_argument("query", help="Question or task to retrieve context for.")
    parser.add_argument(
        "--project",
        action="append",
        help="Project slug or alias. Repeat to estimate multiple projects.",
    )
    parser.add_argument(
        "--cwd",
        help="Working directory used for project resolution when --project is omitted.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=5,
        help="Maximum chunks used by retrieved context. Default: 5.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=6_000,
        help="Maximum characters used by retrieved context. Default: 6000.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    projects = args.project or [None]
    rows = [
        estimate_for_project(
            args.query,
            project=project,
            cwd=args.cwd,
            max_chunks=args.max_chunks,
            max_chars=args.max_chars,
        )
        for project in projects
    ]

    if args.json:
        print(json.dumps({"query": args.query, "projects": rows}, indent=2))
        return

    print(f"query={args.query!r}")
    for row in rows:
        print(
            f"project={row['project_slug']} files={row['files']} chunks={row['chunks']} "
            f"indexed_tokens={row['indexed_tokens']} "
            f"retrieved_tokens={row['retrieved_tokens']} "
            f"saved_tokens={row['saved_tokens']} "
            f"savings={row['savings_percent']}%"
        )
    print(
        "note=Estimate compares all indexed chunk tokens for a project against "
        "the compact context block returned for this query."
    )


def estimate_for_project(
    query: str,
    *,
    project: str | None,
    cwd: str | None,
    max_chunks: int,
    max_chars: int,
) -> dict[str, Any]:
    resolved = resolve_project(project=project, cwd=cwd)
    stats = indexed_token_stats(str(resolved["id"]))
    _, _, context = compose_project_context(
        query,
        project=resolved["slug"],
        max_chunks=max_chunks,
        max_chars=max_chars,
    )
    retrieved_tokens = count_tokens(context)
    indexed_tokens = stats["indexed_tokens"]
    saved_tokens = max(0, indexed_tokens - retrieved_tokens)
    savings_percent = 0.0
    if indexed_tokens > 0:
        savings_percent = round((saved_tokens / indexed_tokens) * 100, 2)

    return {
        "project_slug": resolved["slug"],
        "project_name": resolved["name"],
        "files": stats["files"],
        "chunks": stats["chunks"],
        "indexed_tokens": indexed_tokens,
        "retrieved_tokens": retrieved_tokens,
        "saved_tokens": saved_tokens,
        "savings_percent": savings_percent,
    }


def indexed_token_stats(project_id: str) -> dict[str, int]:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            select
                count(distinct f.id) as files,
                count(c.id) as chunks,
                coalesce(sum(c.token_count), 0) as indexed_tokens
            from gpc_chunks c
            left join gpc_files f on f.id = c.file_id
            where c.project_id = %s
              and c.source_type = 'project_file'
            """,
            (project_id,),
        ).fetchone()
    return {
        "files": int(row["files"] or 0),
        "chunks": int(row["chunks"] or 0),
        "indexed_tokens": int(row["indexed_tokens"] or 0),
    }


def count_tokens(text: str) -> int:
    encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


if __name__ == "__main__":
    main()
