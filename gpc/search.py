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
    include_graph: bool = False,
    graph_min_confidence: str = "EXTRACTED",
) -> tuple[dict[str, Any], list[SearchResult], str]:
    """Compose a bounded context block from semantic retrieval.

    When ``include_graph=True`` and the project has a Graphify projection in
    Neo4j, each retrieved chunk is augmented with a short structural footer
    listing its immediate neighbours in the graph (callers/callees, same-file
    bridges). The footer is *appended* to the chunk without changing the
    ranking, so hybrid mode never hides evidence. Confidence on neighbours is
    exposed explicitly.
    """

    chunk_limit = max(1, min(max_chunks, 20))
    char_budget = max(1_000, min(max_chars, 30_000))
    resolved_project, results = search_project_context(
        query,
        project=project,
        cwd=cwd,
        limit=chunk_limit,
        repo=repo,
    )

    graph_notes_by_index: dict[int, str] = {}
    if include_graph and results:
        graph_notes_by_index = _graph_annotations(
            project_slug=resolved_project["slug"],
            results=results,
            min_confidence=graph_min_confidence,
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
        footer = graph_notes_by_index.get(index, "")
        overhead = len(source_header) + 2 + (len(footer) + 2 if footer else 0)
        block_budget = max(0, remaining - overhead)
        if block_budget <= 0:
            break
        if len(content) > block_budget:
            content = f"{content[: max(0, block_budget - 3)].rstrip()}..."

        block = f"{source_header}\n{content}"
        if footer:
            block = f"{block}\n{footer}"
        parts.append(block)
        remaining -= len(block) + 1

    return resolved_project, results, "\n".join(parts).strip()


def _graph_annotations(
    *,
    project_slug: str,
    results: list[SearchResult],
    min_confidence: str,
) -> dict[int, str]:
    """Look up Graphify neighbours for each retrieved chunk's source file.

    Never raise — if Neo4j is unreachable or the projection is missing, this
    function returns an empty map and the caller falls back to pure semantic
    retrieval.
    """

    try:
        from gpc.graph_query import graph_neighbors
    except Exception:  # noqa: BLE001
        return {}

    out: dict[int, str] = {}
    seen_source_files: set[tuple[str | None, str]] = set()
    for index, result in enumerate(results, start=1):
        key = (result.repo_slug, result.relative_path)
        if key in seen_source_files:
            continue
        seen_source_files.add(key)
        query_term = result.relative_path
        try:
            data = graph_neighbors(
                project_slug,
                query_term,
                depth=1,
                min_confidence=min_confidence,
                limit=5,
            )
        except Exception:  # noqa: BLE001
            continue
        neighbors = data.get("neighbors") or []
        if not neighbors:
            continue
        lines = ["  Graph neighbours (confidence tagged):"]
        for neighbor in neighbors:
            edge = (neighbor.get("edges") or [{}])[0]
            relation = edge.get("relation", "?")
            conf = edge.get("confidence") or "EXTRACTED"
            rule = edge.get("rule")
            rule_tag = f" via {rule}" if rule else ""
            lines.append(
                f"    - [{neighbor.get('repo_slug')}] "
                f"{neighbor.get('label')} "
                f"({relation}{rule_tag}, {conf})"
            )
        out[index] = "\n".join(lines)
    return out


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
