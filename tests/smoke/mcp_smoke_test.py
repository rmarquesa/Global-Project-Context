from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    server = StdioServerParameters(
        command=str(ROOT / "venv" / "bin" / "python"),
        args=[str(ROOT / "gpc_mcp_server.py")],
        cwd=ROOT,
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = {
                "gpc.health",
                "gpc.resolve_project",
                "gpc.resolve_repo",
                "gpc.list_projects",
                "gpc.list_repos",
                "gpc.index_status",
                "gpc.search",
                "gpc.context",
                "gpc.estimate_token_savings",
                "gpc.graph_neighbors",
                "gpc.graph_summary",
                "gpc.graph_path",
                "gpc.graph_community",
                "gpc.graph_diff",
                "gpc.drift_signals",
                "gpc.self_metrics",
                "gpc.mcp_usage",
            }
            missing = sorted(expected - set(names))
            if missing:
                raise SystemExit(f"Missing MCP tools: {missing}")

            health = await session.call_tool("gpc.health", {})
            health_payload = _json_payload(health)
            if not health_payload.get("ok"):
                raise SystemExit(f"Health check failed: {health_payload}")

            # Pick the first project that has both repos and chunks — this
            # isolates the smoke from projects created by other tests that
            # were never indexed. Explicit ``project`` wins over cwd per
            # the documented priority order, so we can rely on the slug
            # alone without having to thread a fake cwd through every call.
            projects = await session.call_tool("gpc.list_projects", {})
            projects_payload = _json_payload(projects)
            registered = [p for p in projects_payload.get("projects", [])]
            if not registered:
                raise SystemExit("No projects registered — cannot smoke-test MCP tools.")

            probe_slug = None
            probe_cwd = None
            for candidate in registered:
                slug = candidate.get("slug")
                repos = candidate.get("repos") or []
                if not repos:
                    continue
                status = await session.call_tool(
                    "gpc.index_status",
                    {"project": slug, "runs": 1},
                )
                status_payload = _json_payload(status)
                if status_payload.get("status", {}).get("chunks", 0) >= 1:
                    probe_slug = slug
                    # Keep a real cwd handy for the `resolve_repo` probe
                    # that validates repo-level resolution end-to-end.
                    probe_cwd = repos[0].get("root_path")
                    break
            if not probe_slug:
                raise SystemExit(
                    f"No registered project has repos + chunks: {[p.get('slug') for p in registered]}"
                )

            resolved = await session.call_tool(
                "gpc.resolve_project",
                {"project": probe_slug},
            )
            resolved_payload = _json_payload(resolved)
            resolved_slug = resolved_payload.get("project", {}).get("slug")
            if resolved_slug != probe_slug:
                raise SystemExit(
                    f"Explicit project slug {probe_slug!r} did not win resolution: {resolved_payload}"
                )

            # Regression guard: explicit project must beat a cwd pointing at
            # *another* registered project. Pick the MCP server's own cwd
            # (the GPC repo) as the "wrong" cwd. If the fix in
            # resolve_project regresses, this assertion fails loudly.
            other_project = next(
                (p["slug"] for p in registered if p["slug"] != probe_slug),
                None,
            )
            if other_project:
                conflict = await session.call_tool(
                    "gpc.resolve_project",
                    {"project": probe_slug, "cwd": str(ROOT)},
                )
                conflict_payload = _json_payload(conflict)
                conflict_slug = conflict_payload.get("project", {}).get("slug")
                if conflict_slug != probe_slug:
                    raise SystemExit(
                        "resolve_project priority regression: explicit "
                        f"project={probe_slug!r} was shadowed by cwd={ROOT!s} "
                        f"→ resolved to {conflict_slug!r}"
                    )

            repos = await session.call_tool(
                "gpc.list_repos",
                {"project": probe_slug},
            )
            repos_payload = _json_payload(repos)
            if not repos_payload.get("repos"):
                raise SystemExit(f"list_repos returned no repos: {repos_payload}")

            resolved_repo = await session.call_tool(
                "gpc.resolve_repo",
                {"project": probe_slug},
            )
            resolved_repo_payload = _json_payload(resolved_repo)
            if resolved_repo_payload.get("project", {}).get("slug") != probe_slug:
                raise SystemExit(f"resolve_repo failed: {resolved_repo_payload}")

            status = await session.call_tool(
                "gpc.index_status",
                {"project": probe_slug, "runs": 1},
            )
            status_payload = _json_payload(status)
            if status_payload.get("status", {}).get("chunks", 0) < 1:
                raise SystemExit(f"Index status did not return chunks: {status_payload}")

            search = await session.call_tool(
                "gpc.search",
                {
                    "project": probe_slug,
                    "query": "authentication",
                    "limit": 2,
                    "content_chars": 300,
                },
            )
            search_payload = _json_payload(search)
            if not search_payload.get("results"):
                raise SystemExit(f"Search did not return results: {search_payload}")

            context = await session.call_tool(
                "gpc.context",
                {
                    "project": probe_slug,
                    "query": "authentication",
                    "max_chunks": 2,
                    "max_chars": 2_000,
                },
            )
            context_payload = _json_payload(context)
            if not context_payload.get("context"):
                raise SystemExit(f"Context did not return text: {context_payload}")

            estimate = await session.call_tool(
                "gpc.estimate_token_savings",
                {
                    "project": probe_slug,
                    "query": "authentication",
                    "max_chunks": 2,
                    "max_chars": 2_000,
                },
            )
            estimate_payload = _json_payload(estimate)
            if estimate_payload.get("estimate", {}).get("retrieved_tokens", 0) < 1:
                raise SystemExit(f"Token estimate failed: {estimate_payload}")

            # graph tools: shape check only — don't assume any particular
            # project has a Graphify projection in every environment.
            summary = await session.call_tool(
                "gpc.graph_summary",
                {"project": probe_slug, "top_k_gods": 3, "include_cohesion": False},
            )
            summary_payload = _json_payload(summary)
            if not summary_payload.get("ok"):
                raise SystemExit(f"graph_summary failed: {summary_payload}")

            neighbors = await session.call_tool(
                "gpc.graph_neighbors",
                {
                    "project": probe_slug,
                    "node": "main",
                    "depth": 1,
                    "min_confidence": "EXTRACTED",
                    "limit": 5,
                },
            )
            neighbors_payload = _json_payload(neighbors)
            if not neighbors_payload.get("ok"):
                raise SystemExit(f"graph_neighbors failed: {neighbors_payload}")

            path_result = await session.call_tool(
                "gpc.graph_path",
                {
                    "project": probe_slug,
                    "a": "main",
                    "b": "auth",
                    "max_hops": 4,
                    "min_confidence": "INFERRED",
                },
            )
            path_payload = _json_payload(path_result)
            if not path_payload.get("ok"):
                raise SystemExit(f"graph_path failed: {path_payload}")

            usage = await session.call_tool(
                "gpc.mcp_usage",
                {"window_hours": 1},
            )
            usage_payload = _json_payload(usage)
            if not usage_payload.get("ok"):
                raise SystemExit(f"mcp_usage failed: {usage_payload}")
            # The calls we just made should show up in the aggregate.
            if usage_payload.get("totals", {}).get("total", 0) < 1:
                raise SystemExit(f"mcp_usage reported no calls: {usage_payload}")

            print(f"tools={','.join(names)}")
            print("mcp_smoke_test=passed")


def _json_payload(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content

    if not result.content:
        return {}

    text = getattr(result.content[0], "text", "")
    return json.loads(text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
