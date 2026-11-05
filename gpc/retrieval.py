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
LEXICAL_STRATEGIES = ("legacy", "weighted")
WEIGHTED_POLICY_VERSION = "weighted-v1"
WEIGHTED_TIE_POLICY = "experimental:score-desc,repo,path,chunk_index,content_hash,id"
# Fixed English/Portuguese function words only: articles, pronouns,
# prepositions and auxiliaries. Exact matches only; no domain vocabulary.
# This exact set and weights are frozen before evaluation for weighted-v1.
WEIGHTED_STOPWORDS = frozenset(
    "a an the i me my we us our you your he him his she her it its they them their "
    "of in on at to for from by with as is are was were be been being am do does "
    "did have has had can could will would shall should may might must "
    "o os a as um uma uns umas eu me mim meu minha nós nos nosso nossa você vocês "
    "ele ela eles elas seu sua seus suas de do da dos das em no na nos nas ao aos "
    "à às para por pelo pela pelos pelas com sem entre sobre sob até é são era "
    "eram ser sido sendo estou está estamos estão estar estava estavam tenho tem "
    "temos têm ter tinha tinham foi foram fui fomos pode podem poderia poderiam".split()
)


def weighted_policy_metadata() -> dict[str, str]:
    return {
        "lexical_strategy": "weighted",
        "lexical_policy_version": WEIGHTED_POLICY_VERSION,
        "lexical_tie_policy": WEIGHTED_TIE_POLICY,
    }


def validate_lexical_strategy(strategy: str, mode: str) -> None:
    if strategy not in LEXICAL_STRATEGIES:
        raise ValueError(f"lexical_strategy must be one of {LEXICAL_STRATEGIES}")
    if strategy == "weighted" and mode == "semantic":
        raise ValueError("weighted lexical_strategy is not applicable to semantic mode")


def weighted_query_terms(terms: list[str]) -> list[str]:
    """Deduplicate case-insensitively, keeping literal spelling and punctuation.

    A sole distinct token is retained even if a function word (e.g. identifier
    `in`). Multi-token queries drop only exact function-word matches; an empty
    result intentionally retrieves nothing, never the broad legacy fallback.
    """
    distinct = {}
    for term in terms:
        distinct.setdefault(term.casefold(), term)
    if len(distinct) == 1:
        return list(distinct.values())
    return [term for key, term in distinct.items() if key not in WEIGHTED_STOPWORDS]


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
    lexical_strategy: str = "legacy",
) -> list[tuple[str, float]]:
    """Return only bounded, SQL-scoped IDs; never load the corpus into Python."""
    validate_lexical_strategy(lexical_strategy, "lexical")
    if lexical_strategy == "weighted":
        return _weighted_candidates(
            terms, project_id=project_id, repo_slugs=repo_slugs, limit=limit
        )
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


def _weighted_candidates(
    terms: list[str], *, project_id: str, repo_slugs: list[str] | None, limit: int
) -> list[tuple[str, float]]:
    """Sum each distinct literal term's strongest field: path 6/title 3/body 1.

    No full-text bonus, frequency multiplier, stemming or corpus download.
    Ownership is identical to legacy; only bounded IDs/scores leave Postgres.
    """
    patterns = [_literal_pattern(term) for term in weighted_query_terms(terms)]
    if not patterns:
        return []
    sql = rf"""
        with scoped as (
            select c.id, f.relative_path, c.title, c.content, r.slug as repo_slug,
                   c.chunk_index, c.content_hash
            {SCOPED_CHUNKS}
        ), scored as (
            select scoped.*, (
                select sum(case
                    when coalesce(relative_path, '') ilike terms.pattern escape E'\\' then 6
                    when coalesce(title, '') ilike terms.pattern escape E'\\' then 3
                    when content ilike terms.pattern escape E'\\' then 1
                    else 0 end)
                from unnest(%s::text[]) as terms(pattern)
            ) as score
            from scoped
        )
        select id::text as id, score from scored
        where score > 0
        order by score desc, repo_slug, relative_path, chunk_index, content_hash, id
        limit %s
    """
    with pg_connection(row_factory=dict_row) as conn:
        rows = conn.execute(
            sql,
            (project_id, repo_slugs, repo_slugs, patterns, min(limit, MAX_CANDIDATES)),
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
