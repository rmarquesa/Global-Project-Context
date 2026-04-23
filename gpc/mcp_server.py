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
from gpc.registry import list_projects, resolve_project
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
def mcp_list_projects() -> dict[str, Any]:
    """List projects registered in GPC."""
    try:
        projects = [_project_payload(project) for project in list_projects()]
        return {"ok": True, "projects": projects, "count": len(projects)}
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.index_status")
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
def mcp_search(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    limit: int = 5,
    content_chars: int = 1_200,
) -> dict[str, Any]:
    """Search indexed project chunks with Ollama embeddings and Qdrant."""
    try:
        safe_limit = max(1, min(limit, 20))
        safe_chars = max(200, min(content_chars, 8_000))
        resolved_project, results = search_project_context(
            query,
            project=project,
            cwd=_effective_cwd(cwd),
            limit=safe_limit,
        )
        return {
            "ok": True,
            "project": _project_payload(resolved_project),
            "query": query,
            "results": [
                _search_result_payload(result, content_chars=safe_chars)
                for result in results
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.context")
def mcp_context(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    max_chunks: int = 5,
    max_chars: int = 6_000,
) -> dict[str, Any]:
    """Return a bounded context block suitable for an AI model prompt."""
    try:
        resolved_project, results, context = compose_project_context(
            query,
            project=project,
            cwd=_effective_cwd(cwd),
            max_chunks=max_chunks,
            max_chars=max_chars,
        )
        return {
            "ok": True,
            "project": _project_payload(resolved_project),
            "query": query,
            "context": context,
            "sources": [
                _search_result_payload(result, content_chars=0)
                for result in results
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": _error_payload(exc)}


@mcp.tool(name="gpc.estimate_token_savings")
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
