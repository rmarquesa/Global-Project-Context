"""Bounded lexical candidates and deterministic, retrieval-only ranking helpers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from psycopg.rows import dict_row

from gpc.db import pg_connection

MAX_QUERY_CHARS = 2048
MAX_QUERY_TERMS = 32
MAX_CANDIDATES = 300
RRF_K = 60

# Keep the expression identical to migrations/0010_lexical_search.sql.
LEXICAL_VECTOR = (
    "to_tsvector('simple'::regconfig, coalesce(c.title, '') || ' ' || c.content)"
)

# A vector payload is only a candidate hint, never an authorization decision.
# Recover legacy chunks via the file's repo, but reject conflicting ownership.
SCOPED_CHUNKS = """
    from gpc_chunks c
    join gpc_files f on f.id = c.file_id and f.project_id = c.project_id
    join gpc_repos r on r.id = coalesce(c.repo_id, f.repo_id)
      and r.project_id = c.project_id
      and (f.repo_id is null or f.repo_id = r.id)
    where c.project_id = %s::uuid
      and c.source_type = 'project_file'
      and (%s::text[] is null or r.slug = any(%s::text[]))
"""


def query_terms(query: str) -> list[str]:
    """Preserve literal paths/identifiers, including Unicode and punctuation.

    Plain full text also tokenizes punctuation; literal matching preserves
    camelCase and snake_case spelling rather than relying on tokenization.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters")
    if "\x00" in query:
        raise ValueError("query must not contain NUL characters")
    # Recognize camelCase/snake_case identifiers and slash/dot paths even when
    # wrapped in Markdown backticks, quotes, or function-call punctuation.
    tokens = re.findall(r"[\w./\\%:-]+", query)
    if len(query.split()) > MAX_QUERY_TERMS or len(tokens) > MAX_QUERY_TERMS:
        raise ValueError(f"query must contain at most {MAX_QUERY_TERMS} terms")
    return list(dict.fromkeys(tokens)) or [query.strip()]


def _literal_pattern(term: str) -> str:
    return (
        "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


def lexical_candidates(
    query: str,
    terms: list[str],
    *,
    project_id: str,
    repo_slugs: list[str] | None,
    limit: int,
) -> list[tuple[str, float]]:
    """Return only bounded, SQL-scoped IDs; never load the corpus into Python."""
    patterns = [_literal_pattern(term) for term in terms]
    # EXISTS avoids multiplying candidates when several literal terms match.
    # ESCAPE is explicit so user '%'/'_' never broaden a literal lookup.
    literal = r"""
        exists (
            select 1 from unnest(%s::text[]) as terms(pattern)
            where coalesce(f.relative_path, '') ilike terms.pattern escape E'\\'
               or coalesce(c.title, '') ilike terms.pattern escape E'\\'
               or c.content ilike terms.pattern escape E'\\'
        )
    """
    sql = f"""
        select c.id::text as id,
          (ts_rank_cd({LEXICAL_VECTOR}, plainto_tsquery('simple', %s))
            + case when {literal} then 1.0 else 0.0 end) as score
        {SCOPED_CHUNKS}
          and ({LEXICAL_VECTOR} @@ plainto_tsquery('simple', %s)
               or {literal})
        order by score desc, c.id asc
        limit %s
    """
    with pg_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            sql,
            (
                query,
                patterns,
                project_id,
                repo_slugs,
                repo_slugs,
                query,
                patterns,
                min(limit, MAX_CANDIDATES),
            ),
        ).fetchall()
    return [(str(row["id"]), float(row["score"])) for row in rows]


def task_preference(chunk: dict[str, Any], task: str) -> tuple[float, str]:
    """A modest 5% preference, not a filter or a learned reranker."""
    path = PurePosixPath(chunk["relative_path"].replace("\\", "/").lower())
    is_test = bool(
        set(path.parts) & {"test", "tests", "__tests__", "spec", "specs"}
        or re.search(r"(^test_|_test\.|\.test\.|\.spec\.|^test[^/]*\.)", path.name)
    )
    is_code = path.suffix in {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".php",
        ".swift",
        ".kt",
        ".sh",
        ".sql",
    }
    is_impl = is_code and not is_test
    is_config = path.suffix in {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
    } or path.name in {"dockerfile", "makefile"}
    preferred = {
        "general": False,
        "test": is_test,
        "debug": is_impl or is_test,
        "implement": is_impl,
        "review": is_code or is_test or is_config,
    }[task]
    kind = (
        "test"
        if is_test
        else "implementation" if is_impl else "config" if is_config else "other"
    )
    return (
        1.05 if preferred else 1.0
    ), f"task:{task}:{kind}:boost={1.05 if preferred else 1.0:g}"
