from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gpc.config import COLLECTION_NAME
from gpc.db import get_qdrant, pg_connection
from gpc.embeddings import embed_query
from gpc.registry import normalize_slug, resolve_project
from gpc.retrieval import (
    MAX_CANDIDATES,
    RRF_K,
    SCOPED_CHUNKS,
    lexical_candidates,
    query_terms,
    task_preference,
)

SEARCH_MODES = ("semantic", "lexical", "hybrid")
SEARCH_TASKS = ("general", "debug", "implement", "review", "test")


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
    graph_notes: str | None = None
    retrieval_sources: tuple[str, ...] = ()
    ranking_signals: tuple[str, ...] = ()


def search_project_context(
    query: str,
    *,
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 5,
    repo: str | list[str] | None = None,
    mode: str = "semantic",
    task: str = "general",
    candidate_trace: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[SearchResult]]:
    """Retrieve bounded evidence, scoped authoritatively by Postgres ownership.

    Semantic retains vector order/scores (task preferences are ignored). Lexical
    and hybrid apply a transparent 5% task preference to retrieved candidates
    only. Hybrid scores are raw reciprocal rank fusion (k=60), NOT cosine;
    task preference affects ordering, not the reported raw score. An empty repo
    list means no filter; nonempty lists must contain only valid, nonblank slugs.
    Backend failures propagate: partial hybrid retrieval is never called success.
    candidate_trace opt-in receives a content-free v1 capture of these same
    candidates (see eval_candidates); it is cleared even when retrieval fails.
    """
    if candidate_trace is not None:
        if not isinstance(candidate_trace, dict):
            raise ValueError("candidate_trace must be a dict")
        candidate_trace.clear()
    if mode not in SEARCH_MODES:
        raise ValueError(f"mode must be one of {SEARCH_MODES}")
    if task not in SEARCH_TASKS:
        raise ValueError(f"task must be one of {SEARCH_TASKS}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    terms = query_terms(query)
    repo_slugs = _normalize_repos(repo)
    resolved_project = resolve_project(project=project, cwd=cwd)
    project_id = str(resolved_project["id"])
    candidate_limit = min(limit * 3, MAX_CANDIDATES)
    channels: dict[str, list[tuple[str, float]]] = {}
    if mode in ("semantic", "hybrid"):
        channels["semantic"] = _semantic_candidates(
            query, project_id=project_id, repo_slugs=repo_slugs, limit=candidate_limit
        )
    if mode in ("lexical", "hybrid"):
        channels["lexical"] = lexical_candidates(
            query,
            terms,
            project_id=project_id,
            repo_slugs=repo_slugs,
            limit=candidate_limit,
        )

    scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    signals: dict[str, list[str]] = {}
    for channel, candidates in channels.items():
        seen = set()
        for chunk_id, score in candidates:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + (
                1.0 / (RRF_K + rank) if mode == "hybrid" else score
            )
            sources.setdefault(chunk_id, []).append(channel)
            signals.setdefault(chunk_id, []).append(f"{channel}:rank={rank}")

    if not scores:
        if candidate_trace is not None:
            from gpc.eval_candidates import build_capture

            candidate_trace.update(
                build_capture(
                    channels,
                    [],
                    mode=mode,
                    task=task,
                    k=limit,
                    candidate_limit=candidate_limit,
                )
            )
        return resolved_project, []
    chunks = _fetch_chunks(list(scores), project_id=project_id, repo_slugs=repo_slugs)
    chunk_by_id = {str(chunk["id"]): chunk for chunk in chunks}
    order = {chunk_id: index for index, chunk_id in enumerate(scores)}
    ranked = []
    for chunk_id, score in scores.items():
        chunk = chunk_by_id.get(chunk_id)
        if not chunk:
            continue
        factor = 1.0
        if mode == "hybrid":
            signals[chunk_id].append(f"rrf:k={RRF_K}")
        if mode != "semantic" and task != "general":
            factor, signal = task_preference(chunk, task)
            signals[chunk_id].append(signal)
        elif mode == "semantic" and task != "general":
            signals[chunk_id].append(f"task:{task}:ignored-semantic")
        ranked.append(
            (
                factor * score,
                SearchResult(
                    score=score,
                    chunk_id=chunk_id,
                    relative_path=chunk["relative_path"],
                    title=chunk["title"],
                    content=chunk["content"],
                    chunk_type=chunk["chunk_type"],
                    language=chunk["language"],
                    repo_slug=chunk.get("repo_slug"),
                    retrieval_sources=tuple(sources[chunk_id]),
                    ranking_signals=tuple(signals[chunk_id]),
                ),
            )
        )
    if mode != "semantic":
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
    else:
        ranked.sort(key=lambda item: order[item[1].chunk_id])
    if candidate_trace is not None:
        from gpc.eval_candidates import build_capture

        candidate_trace.update(
            build_capture(
                channels,
                ranked,
                mode=mode,
                task=task,
                k=limit,
                candidate_limit=candidate_limit,
            )
        )
    return resolved_project, [result for _, result in ranked[:limit]]


def _normalize_repos(repo: str | list[str] | None) -> list[str] | None:
    if repo is None or repo == []:
        return None
    if not isinstance(repo, (str, list)):
        raise ValueError("repo must be a slug or list of slugs")
    values = [repo] if isinstance(repo, str) else repo
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("repo filters must contain nonblank slugs")
    return list(dict.fromkeys(normalize_slug(value) for value in values))


def _semantic_candidates(
    query: str, *, project_id: str, repo_slugs: list[str] | None, limit: int
) -> list[tuple[str, float]]:
    query_vector = embed_query(query)

    must = [
        FieldCondition(
            key="project_id",
            match=MatchValue(value=project_id),
        ),
        FieldCondition(
            key="source_type",
            match=MatchValue(value="project_file"),
        ),
    ]

    if repo_slugs:
        from qdrant_client.models import MatchAny

        must.append(
            FieldCondition(
                key="repo_slug",
                match=MatchAny(any=repo_slugs),
            )
        )

    qdrant = get_qdrant()
    response = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=Filter(must=must),
        limit=limit,
        with_payload=True,
    )

    scored_ids: list[tuple[str, float]] = []
    for point in response.points:
        payload = point.payload or {}
        chunk_id = payload.get("chunk_id")
        try:
            chunk_id = str(UUID(str(chunk_id)))
        except (ValueError, TypeError, AttributeError):
            continue
        scored_ids.append((chunk_id, point.score))
    return scored_ids


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
    mode: str = "semantic",
    task: str = "general",
) -> tuple[dict[str, Any], list[SearchResult], str]:
    """Compose a bounded context block from the selected retrieval mode.

    When ``include_graph=True`` and the project has a Graphify projection in
    Neo4j, each retrieved chunk is augmented with a short structural footer
    listing its immediate neighbours in the graph (callers/callees, same-file
    bridges). The footer is *appended* to the chunk without changing the
    ranking, so hybrid mode never hides evidence. Confidence on neighbours is
    exposed explicitly.
    """

    chunk_limit = max(1, min(max_chunks, 20))
    char_budget = max(1_000, min(max_chars, 30_000))
    retrieval_kwargs: dict[str, Any] = {}
    if mode != "semantic":
        retrieval_kwargs["mode"] = mode
    if task != "general":
        retrieval_kwargs["task"] = task
    resolved_project, results = search_project_context(
        query,
        project=project,
        cwd=cwd,
        limit=chunk_limit,
        repo=repo,
        **retrieval_kwargs,
    )

    if include_graph and results:
        results = annotate_search_results(
            resolved_project["slug"], results, min_confidence=graph_min_confidence
        )

    header = [
        f"Project: {resolved_project['slug']} ({resolved_project['name']})",
        f"Query: {query}",
        "",
        "Relevant context:",
    ]
    prefix = "\n".join(header)
    notice = "\n...[truncated]"
    if len(prefix) + len(notice) > char_budget:
        # Metadata is not evidence; explicitly compact it before adding sources.
        def compact(text: str) -> str:
            return text if len(text) <= 100 else text[:85] + "...[truncated]"

        prefix = (
            f"Project: {compact(resolved_project['slug'])} "
            f"({compact(resolved_project['name'])})\n"
            f"Query: {compact(query)}\n\nRelevant context:"
        )
        omitted = True
    else:
        omitted = False
    remaining = char_budget - len(prefix) - len(notice)
    parts = [prefix]

    for index, result in enumerate(results, start=1):
        source_header = (
            f"\n[{index}] {result.relative_path} "
            f"(repo={result.repo_slug or 'unspecified'}, "
            f"score={result.score:.4f}, type={result.chunk_type})"
        )
        content = result.content.strip()
        footer = result.graph_notes or ""
        # Include the separators in the budget, and prefer content to graph notes.
        block_budget = remaining - len(source_header) - 2
        if block_budget < 16:
            omitted = True
            break
        if len(content) + (len(footer) + 1 if footer else 0) > block_budget:
            omitted = True
            footer = ""
        if len(content) > block_budget:
            content = f"{content[: max(0, block_budget - 3)].rstrip()}..."

        block = f"{source_header}\n{content}"
        if footer:
            block = f"{block}\n{footer}"
        parts.append(block)
        remaining -= len(block) + 1

    block = "\n".join(parts).strip() + (notice if omitted else "")
    return resolved_project, results, block


def annotate_search_results(
    project_slug: str, results: list[SearchResult], *, min_confidence: str = "EXTRACTED"
) -> list[SearchResult]:
    """Enrich the same ranked results, without another semantic search."""
    annotations = _graph_annotations(
        project_slug=project_slug, results=results, min_confidence=min_confidence
    )
    return [
        replace(result, graph_notes=annotations.get(index))
        for index, result in enumerate(results, 1)
    ]


def _graph_source_id(project_slug: str, result: SearchResult) -> str | None:
    """Resolve a file inside its project AND repository before graph traversal."""
    from gpc.graph import neo4j_driver

    with neo4j_driver() as driver:
        with driver.session() as session:
            record = session.run(
                """
                MATCH (n)
                WHERE (n:GraphifyNode OR n:GPCEntity)
                  AND n.project_slug = $project
                  AND (n.repo_slug = $repo OR ($repo IS NULL AND n.repo_slug IS NULL))
                  AND coalesce(n.source_file, n.relative_path) = $path
                RETURN n.id AS id
                ORDER BY n.id LIMIT 1
                """,
                project=project_slug,
                repo=result.repo_slug,
                path=result.relative_path,
            ).single()
    return record["id"] if record else None


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
    notes_by_source: dict[tuple[str | None, str], str | None] = {}
    for index, result in enumerate(results, start=1):
        key = (result.repo_slug, result.relative_path)
        if key in notes_by_source:
            if notes_by_source[key]:
                out[index] = notes_by_source[key]
            continue
        notes_by_source[key] = None
        try:
            query_term = _graph_source_id(project_slug, result)
            if not query_term:
                continue
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
        notes_by_source[key] = out[index]
    return out


def _fetch_chunks(
    chunk_ids: list[str], *, project_id: str, repo_slugs: list[str] | None
) -> list[dict[str, Any]]:
    with pg_connection(row_factory=dict_row) as conn:
        return conn.execute(
            f"""
            select
                c.id::text as id,
                c.title,
                c.content,
                c.chunk_type,
                f.relative_path,
                f.language,
                r.slug as repo_slug
            {SCOPED_CHUNKS}
              and c.id = any(%s::uuid[])
            """,
            (project_id, repo_slugs, repo_slugs, chunk_ids),
        ).fetchall()
