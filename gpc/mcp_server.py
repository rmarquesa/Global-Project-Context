from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient

from gpc.config import (
    COLLECTION_NAME,
    MCP_HTTP_HOST,
    MCP_HTTP_PATH,
    MCP_HTTP_PORT,
    POSTGRES_DSN,
    QDRANT_HOST,
    QDRANT_PORT,
)
from gpc.embeddings import active_embedding_model, embedding_dimension
from gpc.graph_query import (
    ALLOWED_CONFIDENCES,
    graph_neighbors,
    graph_path,
    graph_summary,
)
from gpc.mcp_observability import log_mcp_call
from gpc.registry import list_projects, list_repos, resolve_project, resolve_repo
from gpc.search import compose_project_context, search_project_context
from gpc.status import get_index_status
from gpc.token_economy import estimate_for_project


mcp = FastMCP(
    "GPC",
    instructions=(
        "Global Project Context. Resolve local projects and retrieve bounded "
        "indexed context from Postgres, Qdrant, and Ollama embeddings. Pass "
        "the active workspace path as cwd, or pass an explicit project slug, "
        "when asking for project-specific context."
    ),
    log_level="ERROR",
    host=MCP_HTTP_HOST,
    port=MCP_HTTP_PORT,
    streamable_http_path=MCP_HTTP_PATH,
)


@mcp.tool(name="gpc.health")
@log_mcp_call("gpc.health")
def health() -> dict[str, Any]:
    """Check whether GPC backing services are reachable."""
    checks: dict[str, Any] = {}

    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute("select 1")
        checks["postgres"] = {"ok": True}
    except Exception as exc:
        checks["postgres"] = _error_payload(exc)

    try:
        qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        info = qdrant.get_collection(COLLECTION_NAME)
        checks["qdrant"] = {
            "ok": True,
            "collection": COLLECTION_NAME,
            "vector_size": getattr(info.config.params.vectors, "size", None),
            "distance": str(getattr(info.config.params.vectors, "distance", "")),
        }
    except Exception as exc:
        checks["qdrant"] = _error_payload(exc)

    try:
        checks["ollama_embeddings"] = {
            "ok": True,
            "model": active_embedding_model(),
            "dimensions": embedding_dimension(),
        }
    except Exception as exc:
        checks["ollama_embeddings"] = _error_payload(exc)

    return {
        "ok": all(check.get("ok") for check in checks.values()),
        "checks": _json_safe(checks),
    }


@mcp.tool(name="gpc.resolve_project")
@log_mcp_call("gpc.resolve_project")
def mcp_resolve_project(
    cwd: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Resolve a project by cwd, slug, or alias."""
    try:
        resolved = resolve_project(cwd=_effective_cwd(cwd), project=project)
        return {"ok": True, "project": _project_payload(resolved)}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.list_projects")
@log_mcp_call("gpc.list_projects")
def mcp_list_projects() -> dict[str, Any]:
    """List projects registered in GPC."""
    try:
        projects = [_project_payload(project) for project in list_projects()]
        # Attach repos to each project so clients see the full picture without
        # needing a second round-trip to gpc.list_repos.
        all_repos = list_repos()
        by_project: dict[str, list[dict[str, Any]]] = {}
        for repo in all_repos:
            by_project.setdefault(repo["project_slug"], []).append(_repo_payload(repo))
        for payload in projects:
            payload["repos"] = by_project.get(payload["slug"], [])
        return {"ok": True, "projects": projects, "count": len(projects)}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.list_repos")
@log_mcp_call("gpc.list_repos")
def mcp_list_repos(project: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    """List repositories under a project. Pass cwd to auto-resolve the project."""
    try:
        if not project and cwd:
            resolved = resolve_project(cwd=_effective_cwd(cwd))
            project = resolved["slug"]
        repos = [_repo_payload(repo) for repo in list_repos(project)]
        return {"ok": True, "repos": repos, "count": len(repos), "project": project}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.resolve_repo")
@log_mcp_call("gpc.resolve_repo")
def mcp_resolve_repo(
    cwd: str | None = None,
    project: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Resolve a (project, repo) tuple from cwd or explicit slugs.

    Prefer this over gpc.resolve_project when the caller knows it is inside a
    specific repo of a multi-repo project and wants the repo scope pre-applied
    to gpc.search / gpc.context.
    """
    try:
        resolved = resolve_repo(cwd=_effective_cwd(cwd), project=project, repo=repo)
        return {
            "ok": True,
            "project": _project_payload(resolved["project"]),
            "repo": _repo_payload(resolved["repo"]) if resolved.get("repo") else None,
            "resolution_reason": resolved.get("resolution_reason"),
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.index_status")
@log_mcp_call("gpc.index_status")
def mcp_index_status(
    project: str | None = None,
    cwd: str | None = None,
    runs: int = 3,
) -> dict[str, Any]:
    """Return indexed file/chunk counts, Qdrant point counts, and recent runs."""
    try:
        status = get_index_status(project=project, cwd=_effective_cwd(cwd), runs=runs)
        status["project"] = _project_payload(status["project"])
        return {"ok": True, "status": _json_safe(status)}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.search")
@log_mcp_call("gpc.search")
def mcp_search(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 5,
    content_chars: int = 1_200,
    repo: str | list[str] | None = None,
) -> dict[str, Any]:
    """Search indexed project chunks with Ollama embeddings and Qdrant.

    Pass ``repo`` (slug or list of slugs) to narrow the search to specific
    repositories of the project.
    """
    try:
        safe_limit = max(1, min(limit, 20))
        safe_chars = max(200, min(content_chars, 8_000))
        resolved_project, results = search_project_context(
            query,
            project=project,
            cwd=_effective_cwd(cwd),
            limit=safe_limit,
            repo=repo,
        )
        return {
            "ok": True,
            "project": _project_payload(resolved_project),
            "query": query,
            "repo_filter": repo,
            "results": [
                _search_result_payload(result, content_chars=safe_chars)
                for result in results
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.context")
@log_mcp_call("gpc.context")
def mcp_context(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    max_chunks: int = 5,
    max_chars: int = 6_000,
    repo: str | list[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded context block suitable for an AI model prompt.

    Pass ``repo`` (slug or list of slugs) to narrow the context to specific
    repositories of the project.
    """
    try:
        resolved_project, results, context = compose_project_context(
            query,
            project=project,
            cwd=_effective_cwd(cwd),
            max_chunks=max_chunks,
            max_chars=max_chars,
            repo=repo,
        )
        return {
            "ok": True,
            "project": _project_payload(resolved_project),
            "query": query,
            "repo_filter": repo,
            "context": context,
            "sources": [
                _search_result_payload(result, content_chars=0)
                for result in results
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.graph_neighbors")
@log_mcp_call("gpc.graph_neighbors")
def mcp_graph_neighbors(
    node: str,
    project: str | None = None,
    cwd: str | None = None,
    depth: int = 1,
    min_confidence: str = "EXTRACTED",
    relations: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return neighbors of a node in the project's graph, with tiered confidence.

    Answers structural questions such as "who calls X?" or "what does X touch?"
    that semantic search cannot express. ``node`` is matched against
    ``GraphifyNode.id`` or ``GraphifyNode.label``. ``min_confidence`` is one of
    ``EXTRACTED``, ``INFERRED``, ``AMBIGUOUS`` (default ``EXTRACTED`` — caller
    must opt in to heuristic edges). ``relations`` filters by relationship
    type (e.g. ``["GRAPHIFY_RELATION"]`` or ``["CROSS_REPO_BRIDGE"]``).
    """
    try:
        if min_confidence not in ALLOWED_CONFIDENCES:
            raise ValueError(
                f"min_confidence must be one of {ALLOWED_CONFIDENCES}, got {min_confidence!r}"
            )
        resolved = resolve_project(project=project, cwd=_effective_cwd(cwd))
        payload = graph_neighbors(
            resolved["slug"],
            node,
            depth=max(1, min(depth, 3)),
            min_confidence=min_confidence,
            relations=relations,
            limit=max(1, min(limit, 200)),
        )
        payload["project"] = _project_payload(resolved)
        return {"ok": True, **payload}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.graph_summary")
@log_mcp_call("gpc.graph_summary")
def mcp_graph_summary(
    project: str | None = None,
    cwd: str | None = None,
    top_k_gods: int = 10,
    include_cohesion: bool = True,
) -> dict[str, Any]:
    """Return god nodes, repo breakdown, cross-repo bridges and communities.

    Use this before reading code: it is the structured equivalent of
    ``graphify-out/GRAPH_REPORT.md`` and gives the caller a mental map of the
    project in one call.
    """
    try:
        resolved = resolve_project(project=project, cwd=_effective_cwd(cwd))
        payload = graph_summary(
            resolved["slug"],
            top_k_gods=max(1, min(top_k_gods, 50)),
            include_cohesion=include_cohesion,
        )
        payload["project"] = _project_payload(resolved)
        return {"ok": True, **payload}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.graph_path")
@log_mcp_call("gpc.graph_path")
def mcp_graph_path(
    a: str,
    b: str,
    project: str | None = None,
    cwd: str | None = None,
    max_hops: int = 6,
    min_confidence: str = "EXTRACTED",
) -> dict[str, Any]:
    """Return the shortest path between two nodes in the project's graph.

    Each hop reports the relation type, direction, confidence, and the rule
    that produced it (for cross-repo bridges). When no path is found or the
    best path falls below ``min_confidence``, ``path`` is ``None`` and
    ``reason`` explains why.
    """
    try:
        if min_confidence not in ALLOWED_CONFIDENCES:
            raise ValueError(
                f"min_confidence must be one of {ALLOWED_CONFIDENCES}, got {min_confidence!r}"
            )
        resolved = resolve_project(project=project, cwd=_effective_cwd(cwd))
        payload = graph_path(
            resolved["slug"],
            a,
            b,
            max_hops=max(1, min(max_hops, 8)),
            min_confidence=min_confidence,
        )
        payload["project"] = _project_payload(resolved)
        return {"ok": True, **payload}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.mcp_usage")
@log_mcp_call("gpc.mcp_usage")
def mcp_usage(
    window_hours: int = 24,
    project: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Report MCP tool-call activity over a time window.

    Use this to confirm that AI clients are actually hitting GPC instead of
    bypassing it with grep / Read. Every other tool logs one row to
    ``gpc_mcp_calls``; this tool aggregates that log.
    """
    try:
        safe_window = max(1, min(int(window_hours), 720))
        resolved = None
        if project or cwd:
            resolved = resolve_project(project=project, cwd=_effective_cwd(cwd) if cwd else None)
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor() as cur:
                totals_row = conn.execute(
                    """
                    select
                        count(*) as total,
                        count(*) filter (where success) as ok,
                        count(*) filter (where not success) as failed,
                        count(distinct client_name) filter (where client_name is not null) as clients,
                        min(called_at) as first_call,
                        max(called_at) as last_call
                    from gpc_mcp_calls
                    where called_at > now() - (%s::text || ' hours')::interval
                      and (%s::uuid is null or project_id = %s::uuid)
                    """,
                    (
                        str(safe_window),
                        str(resolved["id"]) if resolved else None,
                        str(resolved["id"]) if resolved else None,
                    ),
                ).fetchone()
                by_tool = conn.execute(
                    """
                    select tool, count(*) as calls,
                           count(*) filter (where not success) as errors,
                           avg(duration_ms)::int as avg_ms
                    from gpc_mcp_calls
                    where called_at > now() - (%s::text || ' hours')::interval
                      and (%s::uuid is null or project_id = %s::uuid)
                    group by tool
                    order by calls desc
                    """,
                    (
                        str(safe_window),
                        str(resolved["id"]) if resolved else None,
                        str(resolved["id"]) if resolved else None,
                    ),
                ).fetchall()
                by_client = conn.execute(
                    """
                    select coalesce(client_name, 'unknown') as client,
                           count(*) as calls
                    from gpc_mcp_calls
                    where called_at > now() - (%s::text || ' hours')::interval
                      and (%s::uuid is null or project_id = %s::uuid)
                    group by 1 order by calls desc
                    """,
                    (
                        str(safe_window),
                        str(resolved["id"]) if resolved else None,
                        str(resolved["id"]) if resolved else None,
                    ),
                ).fetchall()
                by_project = conn.execute(
                    """
                    select coalesce(project_slug, 'unresolved') as project,
                           count(*) as calls
                    from gpc_mcp_calls
                    where called_at > now() - (%s::text || ' hours')::interval
                    group by 1 order by calls desc
                    """,
                    (str(safe_window),),
                ).fetchall()
        totals = {
            "total": (totals_row[0] if totals_row else 0) or 0,
            "ok": (totals_row[1] if totals_row else 0) or 0,
            "failed": (totals_row[2] if totals_row else 0) or 0,
            "distinct_clients": (totals_row[3] if totals_row else 0) or 0,
            "first_call": str(totals_row[4]) if totals_row and totals_row[4] else None,
            "last_call": str(totals_row[5]) if totals_row and totals_row[5] else None,
        }
        return {
            "ok": True,
            "window_hours": safe_window,
            "project_filter": resolved["slug"] if resolved else None,
            "totals": totals,
            "by_tool": [
                {"tool": row[0], "calls": row[1], "errors": row[2], "avg_ms": row[3]}
                for row in by_tool
            ],
            "by_client": [
                {"client": row[0], "calls": row[1]} for row in by_client
            ],
            "by_project": [
                {"project": row[0], "calls": row[1]} for row in by_project
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.estimate_token_savings")
@log_mcp_call("gpc.estimate_token_savings")
def mcp_estimate_token_savings(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    max_chunks: int = 5,
    max_chars: int = 6_000,
) -> dict[str, Any]:
    """Estimate context token savings for one project and query."""
    try:
        estimate = estimate_for_project(
            query,
            project=project,
            cwd=_effective_cwd(cwd),
            max_chunks=max_chunks,
            max_chars=max_chars,
        )
        return {"ok": True, "estimate": _json_safe(estimate)}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


def _effective_cwd(cwd: str | None) -> str:
    if cwd:
        return cwd

    return os.getenv("GPC_CALLER_CWD") or str(Path.cwd())


def _project_payload(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(project["id"]),
        "slug": project["slug"],
        "name": project["name"],
        "root_path": project["root_path"],
        "description": project.get("description"),
        "primary_language": project.get("primary_language"),
        "aliases": project.get("aliases", []),
        "resolution_reason": project.get("resolution_reason"),
        "resolution_input": project.get("resolution_input"),
    }


def _repo_payload(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(repo["id"]),
        "slug": repo["slug"],
        "name": repo.get("name"),
        "root_path": repo.get("root_path"),
        "description": repo.get("description"),
        "project_slug": repo.get("project_slug"),
        "project_name": repo.get("project_name"),
    }


def _search_result_payload(result: Any, *, content_chars: int) -> dict[str, Any]:
    content = result.content.strip()
    if content_chars <= 0:
        content = ""
    elif len(content) > content_chars:
        content = f"{content[: max(0, content_chars - 3)].rstrip()}..."

    payload = {
        "score": result.score,
        "chunk_id": result.chunk_id,
        "relative_path": result.relative_path,
        "title": result.title,
        "chunk_type": result.chunk_type,
        "language": result.language,
        "repo_slug": getattr(result, "repo_slug", None),
    }
    if content_chars != 0:
        payload["content"] = content
    return payload


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date, Decimal, UUID, Path)):
        return str(value)
    return value


if __name__ == "__main__":
    mcp.run(transport="stdio")
