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
                "gpc.list_projects",
                "gpc.index_status",
                "gpc.search",
                "gpc.context",
                "gpc.estimate_token_savings",
            }
            missing = sorted(expected - set(names))
            if missing:
                raise SystemExit(f"Missing MCP tools: {missing}")

            health = await session.call_tool("gpc.health", {})
            health_payload = _json_payload(health)
            if not health_payload.get("ok"):
                raise SystemExit(f"Health check failed: {health_payload}")

            resolved = await session.call_tool(
                "gpc.resolve_project",
                {"project": "gpc"},
            )
            resolved_payload = _json_payload(resolved)
            if resolved_payload.get("project", {}).get("slug") != "gpc":
                raise SystemExit(f"Project resolution failed: {resolved_payload}")

            status = await session.call_tool(
                "gpc.index_status",
                {"project": "gpc", "runs": 1},
            )
            status_payload = _json_payload(status)
            if status_payload.get("status", {}).get("chunks", 0) < 1:
                raise SystemExit(f"Index status did not return chunks: {status_payload}")

            search = await session.call_tool(
                "gpc.search",
                {
                    "project": "gpc",
                    "query": "como o GPC gera embeddings?",
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
                    "project": "gpc",
                    "query": "como o GPC gera embeddings?",
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
                    "project": "gpc",
                    "query": "como o GPC economiza tokens?",
                    "max_chunks": 2,
                    "max_chars": 2_000,
                },
            )
            estimate_payload = _json_payload(estimate)
            if estimate_payload.get("estimate", {}).get("retrieved_tokens", 0) < 1:
                raise SystemExit(f"Token estimate failed: {estimate_payload}")

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
