"""Smoke test for gpc/mcp_observability.

Exercises a couple of tools directly (no MCP roundtrip), then asserts that
``gpc_mcp_calls`` grew by the expected number of rows and that
``gpc.mcp_usage`` surfaces the right aggregates.
"""

from __future__ import annotations

import psycopg

from gpc.config import POSTGRES_DSN
from gpc.mcp_server import mcp_list_projects, mcp_usage


def _count_calls() -> int:
    with psycopg.connect(POSTGRES_DSN) as conn:
        row = conn.execute("select count(*) from gpc_mcp_calls").fetchone()
    return int(row[0])


def main() -> None:
    before = _count_calls()

    # Exercise two separate tools.
    r = mcp_list_projects()
    assert r["ok"], r
    r = mcp_list_projects()
    assert r["ok"], r

    after = _count_calls()
    assert after - before >= 2, f"Expected 2+ new rows, got {after - before}"

    # mcp_usage itself is a tool → it also logs a row, so aggregate >= 3.
    usage = mcp_usage(window_hours=1)
    assert usage["ok"], usage
    tools_logged = {row["tool"] for row in usage["by_tool"]}
    assert "gpc.list_projects" in tools_logged, usage
    assert usage["totals"]["total"] >= 3, usage

    # Clean up the rows we just inserted so repeat runs stay idempotent.
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute(
            """
            delete from gpc_mcp_calls
            where tool in ('gpc.list_projects', 'gpc.mcp_usage')
              and called_at > now() - interval '5 minutes'
            """
        )

    print("mcp_observability_smoke_test=passed")


if __name__ == "__main__":
    main()
