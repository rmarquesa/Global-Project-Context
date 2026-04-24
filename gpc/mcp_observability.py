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


def _client_name() -> str | None:
    """Identify the calling client when possible.

    Clients like Claude Code, Codex and Copilot don't send a standard "user
    agent" over MCP stdio, but some set environment variables in the server
    process when they spawn it. Fall back to ``GPC_MCP_CLIENT`` for manual
    tagging (set by ``gpc install-clients`` templates in the future).
    """

    for var in (
        "GPC_MCP_CLIENT",
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
) -> None:
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
            conn.execute(
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
                """,
                (
                    tool,
                    project_id,
                    resolved or args.get("project"),
                    args.get("repo") if isinstance(args.get("repo"), str) else None,
                    _client_name(),
                    args.get("cwd") or _caller_cwd(),
                    duration_ms,
                    success,
                    error_type,
                    error_message,
                    Jsonb(args),
                    Jsonb(result_meta),
                ),
            )
    except Exception as exc:  # noqa: BLE001 — never raise from the logger
        # Fall back to stderr so operators can notice observability gaps.
        import sys

        print(
            f"[gpc.mcp.log] failed to log call tool={tool} error={exc!r}",
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
                _write_log(
                    tool=tool_name,
                    args=_filter_args(kwargs),
                    result_meta=_extract_result_meta(result) if isinstance(result, dict) else {},
                    duration_ms=duration_ms,
                    success=success,
                    error_type=error_type,
                    error_message=error_message,
                )

        return wrapper

    return decorator
