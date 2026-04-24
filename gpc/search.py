from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.embeddings import embed_texts
from gpc.registry import normalize_slug, resolve_project


@dataclass(frozen=True)
class SearchResult:
    score: float
    chunk_id: str
    relative_path: str
    title: str | None
    content: str
    chunk_type: str
    language: str | None
    repo_slug: str | None = None


def search_project_context(
    query: str,
    *,
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 5,
    repo: str | list[str] | None = None,
) -> tuple[dict[str, Any], list[SearchResult]]:
    resolved_project = resolve_project(project=project, cwd=cwd)
    batch = embed_texts([query])

    must = [
        FieldCondition(
            key="project_id",
            match=MatchValue(value=str(resolved_project["id"])),
        ),
        FieldCondition(
            key="source_type",
            match=MatchValue(value="project_file"),
        ),
    ]

    if repo:
        from qdrant_client.models import MatchAny

        repo_slugs = [repo] if isinstance(repo, str) else list(repo)
        normalized = [normalize_slug(r) for r in repo_slugs if r]
        if normalized:
            must.append(
                FieldCondition(
                    key="repo_slug",
                    match=MatchAny(any=normalized),
                )
            )

    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    response = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=batch.vectors[0],
        query_filter=Filter(must=must),
        limit=max(limit * 3, limit),
        with_payload=True,
    )

    scored_ids: list[tuple[str, float, str | None]] = []
    for point in response.points:
        payload = point.payload or {}
        chunk_id = payload.get("chunk_id")
        if chunk_id:
            scored_ids.append((chunk_id, point.score, payload.get("repo_slug")))

    if not scored_ids:
        return resolved_project, []

    chunks = _fetch_chunks([chunk_id for chunk_id, _, _ in scored_ids])
    chunk_by_id = {str(chunk["id"]): chunk for chunk in chunks}
    results = []
    for chunk_id, score, repo_slug in scored_ids:
        chunk = chunk_by_id.get(chunk_id)
        if not chunk:
            continue
        results.append(
            SearchResult(
                score=score,
                chunk_id=chunk_id,
                relative_path=chunk["relative_path"],
                title=chunk["title"],
                content=chunk["content"],
                chunk_type=chunk["chunk_type"],
                language=chunk["language"],
                repo_slug=chunk.get("repo_slug") or repo_slug,
            )
        )
        if len(results) >= limit:
            break

    return resolved_project, results


def compose_project_context(
    query: str,
    *,
    project: str | None = None,
    cwd: str | None = None,
    max_chunks: int = 5,
    max_chars: int = 6_000,
    repo: str | list[str] | None = None,
) -> tuple[dict[str, Any], list[SearchResult], str]:
    chunk_limit = max(1, min(max_chunks, 20))
    char_budget = max(1_000, min(max_chars, 30_000))
    resolved_project, results = search_project_context(
        query,
        project=project,
        cwd=cwd,
        limit=chunk_limit,
        repo=repo,
    )

    header = [
        f"Project: {resolved_project['slug']} ({resolved_project['name']})",
        f"Query: {query}",
        "",
        "Relevant context:",
    ]
    remaining = char_budget - sum(len(line) + 1 for line in header)
    parts = header[:]

    for index, result in enumerate(results, start=1):
        source_header = (
            f"\n[{index}] {result.relative_path} "
            f"(score={result.score:.4f}, type={result.chunk_type})"
        )
        content = result.content.strip()
        block_budget = max(0, remaining - len(source_header) - 2)
        if block_budget <= 0:
            break
        if len(content) > block_budget:
            content = f"{content[: max(0, block_budget - 3)].rstrip()}..."

        block = f"{source_header}\n{content}"
        parts.append(block)
        remaining -= len(block) + 1

    return resolved_project, results, "\n".join(parts).strip()


def _fetch_chunks(chunk_ids: list[str]) -> list[dict[str, Any]]:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            """
            select
                c.id::text as id,
                c.title,
                c.content,
                c.chunk_type,
                f.relative_path,
                f.language,
                r.slug as repo_slug
            from gpc_chunks c
            left join gpc_files f on f.id = c.file_id
            left join gpc_repos r on r.id = coalesce(c.repo_id, f.repo_id)
            where c.id = any(%s::uuid[])
            """,
            (chunk_ids,),
        ).fetchall()
