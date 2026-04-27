"""Observability for the MCP server: log every tool invocation to Postgres.

Design:
    * Wrap each tool function via ``log_mcp_call`` so the logging logic stays
      out of the tool body.
    * Never raise from the logger itself — if Postgres is unreachable, log a
      warning and let the tool continue. Breaking MCP calls because of an
      observability failure is worse than losing telemetry.
    * Keep the stored payload small. ``query`` / ``node`` / ``project`` slugs
      are useful to see what clients ask for; raw chunk content and full
      contexts are not logged.
"""

from __future__ import annotations

from functools import wraps
import os
import sys
import time
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN


ARG_WHITELIST = {
    "project",
    "cwd",
    "repo",
    "node",
    "a",
    "b",
    "query",
    "depth",
    "limit",
    "max_hops",
    "max_chunks",
    "max_chars",
    "min_confidence",
    "relations",
    "top_k_gods",
    "include_cohesion",
    "content_chars",
    "runs",
}

ARG_MAX_CHARS = 400
TOKEN_SAVINGS_TOOLS = {"gpc.search", "gpc.context", "gpc.estimate_token_savings"}


def _client_name() -> str | None:
    """Identify the calling client when possible.

    Clients like Claude Code, Codex and Copilot don't send a standard "user
    agent" over MCP stdio, but some set environment variables in the server
    process when they spawn it. Fall back to ``GPC_MCP_CLIENT`` for manual
    tagging (set by ``gpc install-clients`` templates in the future).
    """

    explicit = os.environ.get("GPC_MCP_CLIENT")
    if explicit:
        return explicit.strip().lower()[:80]

    for var in (
        "CLAUDE_CODE_SESSION_ID",  # Claude Code sets this when it spawns stdio servers
        "CODEX_CLI_VERSION",
        "COPILOT_AGENT_VERSION",
    ):
        value = os.environ.get(var)
        if value:
            return var.lower().split("_")[0]
    return None


def _caller_cwd() -> str | None:
    return os.environ.get("GPC_CALLER_CWD")


def _shrink_arg(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > ARG_MAX_CHARS:
            return value[:ARG_MAX_CHARS] + "…"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_shrink_arg(v) for v in value][:20]
    if isinstance(value, dict):
        return {k: _shrink_arg(v) for k, v in list(value.items())[:20]}
    return str(value)[:ARG_MAX_CHARS]


def _filter_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: _shrink_arg(v) for k, v in kwargs.items() if k in ARG_WHITELIST}


def _extract_result_meta(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    meta: dict[str, Any] = {}
    if "ok" in result:
        meta["ok"] = bool(result["ok"])
    if isinstance(result.get("project"), dict):
        proj = result["project"]
        if proj.get("slug"):
            meta["resolved_project"] = proj["slug"]
        if proj.get("resolution_reason"):
            meta["resolution_reason"] = proj["resolution_reason"]
    for key in ("count", "length"):
        if key in result:
            meta[key] = result[key]
    if isinstance(result.get("results"), list):
        meta["results_count"] = len(result["results"])
        meta["returned_chars"] = sum(
            len(str(item.get("content", "")))
            for item in result["results"]
            if isinstance(item, dict)
        )
    if isinstance(result.get("sources"), list):
        meta["sources_count"] = len(result["sources"])
    if isinstance(result.get("context"), str):
        meta["context_chars"] = len(result["context"])
    if isinstance(result.get("estimate"), dict):
        estimate = result["estimate"]
        for key in (
            "indexed_tokens",
            "retrieved_tokens",
            "saved_tokens",
            "savings_percent",
            "files",
            "chunks",
        ):
            if key in estimate:
                meta[key] = estimate[key]
    if isinstance(result.get("neighbors"), list):
        meta["neighbors_count"] = len(result["neighbors"])
    if isinstance(result.get("path"), dict):
        meta["path_length"] = result["path"].get("length")
    return meta


def _write_log(
    *,
    tool: str,
    args: dict[str, Any],
    result_meta: dict[str, Any],
    duration_ms: int,
    success: bool,
    error_type: str | None,
    error_message: str | None,
) -> dict[str, Any] | None:
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            resolved = result_meta.get("resolved_project") if isinstance(result_meta, dict) else None
            project_id = None
            if resolved:
                row = conn.execute(
                    "select id from gpc_projects where slug = %s",
                    (resolved,),
                ).fetchone()
                if row:
                    project_id = row[0]
            client_name = _client_name()
            row = conn.execute(
                """
                insert into gpc_mcp_calls (
                    tool,
                    project_id,
                    project_slug,
                    repo_slug,
                    client_name,
                    client_cwd,
                    duration_ms,
                    success,
                    error_type,
                    error_message,
                    args,
                    result_meta
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    tool,
                    project_id,
                    resolved or args.get("project"),
                    args.get("repo") if isinstance(args.get("repo"), str) else None,
                    client_name,
                    args.get("cwd") or _caller_cwd(),
                    duration_ms,
                    success,
                    error_type,
                    error_message,
                    Jsonb(args),
                    Jsonb(result_meta),
                ),
            ).fetchone()
            return {
                "id": row[0] if row else None,
                "project_id": project_id,
                "project_slug": resolved or args.get("project"),
                "client_name": client_name,
            }
    except Exception as exc:  # noqa: BLE001 — never raise from the logger
        # Fall back to stderr so operators can notice observability gaps.
        print(
            f"[gpc.mcp.log] failed to log call tool={tool} error={exc!r}",
            file=sys.stderr,
        )
    return None


def _int_arg(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _project_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    project = result.get("project")
    if isinstance(project, dict):
        return project
    estimate = result.get("estimate")
    if isinstance(estimate, dict) and estimate.get("project_slug"):
        return {"slug": estimate.get("project_slug"), "name": estimate.get("project_name")}
    return None


def _repo_slug(args: dict[str, Any], result: Any) -> str | None:
    repo = args.get("repo")
    if isinstance(repo, str):
        return repo
    if isinstance(result, dict) and isinstance(result.get("repo_filter"), str):
        return result["repo_filter"]
    return None


def _result_text_for_tokens(tool: str, result: dict[str, Any]) -> str:
    if tool == "gpc.context" and isinstance(result.get("context"), str):
        return result["context"]
    if tool == "gpc.search" and isinstance(result.get("results"), list):
        parts = [
            item.get("content", "")
            for item in result["results"]
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        ]
        return "\n\n".join(parts)
    return ""


def _resolve_project_id(project_slug: str | None) -> Any:
    if not project_slug:
        return None
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            row = conn.execute(
                "select id from gpc_projects where slug = %s",
                (project_slug,),
            ).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def _write_token_savings_sample(
    *,
    tool: str,
    args: dict[str, Any],
    result: Any,
    log_info: dict[str, Any] | None,
) -> None:
    if tool not in TOKEN_SAVINGS_TOOLS or not isinstance(result, dict) or result.get("ok") is not True:
        return

    try:
        project = _project_from_result(result)
        project_slug = (
            (log_info or {}).get("project_slug")
            or (project or {}).get("slug")
            or args.get("project")
        )
        if not isinstance(project_slug, str) or not project_slug:
            return

        project_id = (log_info or {}).get("project_id") or _resolve_project_id(project_slug)
        if not project_id:
            return

        estimate = result.get("estimate") if isinstance(result.get("estimate"), dict) else None
        if estimate:
            indexed_tokens = int(estimate.get("indexed_tokens") or 0)
            retrieved_tokens = int(estimate.get("retrieved_tokens") or 0)
            saved_tokens = int(estimate.get("saved_tokens") or max(indexed_tokens - retrieved_tokens, 0))
            savings_percent = float(estimate.get("savings_percent") or 0)
            returned_chars = 0
            result_count = None
        else:
            from gpc.token_economy import count_tokens, indexed_token_stats

            stats = indexed_token_stats(project_id)
            text = _result_text_for_tokens(tool, result)
            indexed_tokens = int(stats.get("indexed_tokens") or 0)
            retrieved_tokens = count_tokens(text)
            saved_tokens = max(indexed_tokens - retrieved_tokens, 0)
            savings_percent = round((saved_tokens / indexed_tokens) * 100, 2) if indexed_tokens else 0
            returned_chars = len(text)
            if isinstance(result.get("results"), list):
                result_count = len(result["results"])
            elif isinstance(result.get("sources"), list):
                result_count = len(result["sources"])
            else:
                result_count = None

        metadata = {
            "repo_filter": result.get("repo_filter") if isinstance(result, dict) else args.get("repo"),
            "estimate_project_name": estimate.get("project_name") if estimate else None,
        }
        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute(
                """
                insert into gpc_token_savings_samples (
                    mcp_call_id,
                    tool,
                    project_id,
                    project_slug,
                    repo_slug,
                    query,
                    indexed_tokens,
                    retrieved_tokens,
                    saved_tokens,
                    savings_percent,
                    returned_chars,
                    max_chunks,
                    max_chars,
                    result_count,
                    client_name,
                    metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    (log_info or {}).get("id"),
                    tool,
                    project_id,
                    project_slug,
                    _repo_slug(args, result),
                    args.get("query") if isinstance(args.get("query"), str) else None,
                    indexed_tokens,
                    retrieved_tokens,
                    saved_tokens,
                    savings_percent,
                    returned_chars,
                    _int_arg(args, "max_chunks") or _int_arg(args, "limit"),
                    _int_arg(args, "max_chars") or _int_arg(args, "content_chars"),
                    result_count,
                    (log_info or {}).get("client_name") or _client_name(),
                    Jsonb(metadata),
                ),
            )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break MCP calls
        print(
            f"[gpc.token_savings.log] failed to log sample tool={tool} error={exc!r}",
            file=sys.stderr,
        )


def log_mcp_call(tool_name: str) -> Callable:
    """Decorator that logs one row to ``gpc_mcp_calls`` per MCP invocation.

    Placed around ``@mcp.tool(...)`` handlers in ``gpc/mcp_server.py``. The
    wrapper must preserve the original function's signature so FastMCP can
    still introspect it for schema generation.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            success = True
            error_type = None
            error_message = None
            result: Any = None
            try:
                result = func(*args, **kwargs)
                if isinstance(result, dict) and result.get("ok") is False:
                    success = False
                    err = result.get("error") or {}
                    if isinstance(err, dict):
                        error_type = err.get("type")
                        error_message = err.get("message")
                return result
            except Exception as exc:  # noqa: BLE001 — re-raise, but log first
                success = False
                error_type = exc.__class__.__name__
                error_message = str(exc)[:400]
                raise
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                filtered_args = _filter_args(kwargs)
                log_info = _write_log(
                    tool=tool_name,
                    args=filtered_args,
                    result_meta=_extract_result_meta(result) if isinstance(result, dict) else {},
                    duration_ms=duration_ms,
                    success=success,
                    error_type=error_type,
                    error_message=error_message,
                )
                _write_token_savings_sample(
                    tool=tool_name,
                    args=filtered_args,
                    result=result,
                    log_info=log_info,
                )

        return wrapper

    return decorator
