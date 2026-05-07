from __future__ import annotations

from gpc.mcp_observability import redact_sensitive, summarize_mcp_rows


def test_redact_sensitive_recurses_secret_like_fields() -> None:
    payload = {
        "query": "safe",
        "api_key": "abc123secret",
        "nested": {"password": "hunter2", "token_count": 12},
        "items": [{"secret": "value"}],
    }

    redacted = redact_sensitive(payload)

    assert redacted["query"] == "safe"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["token_count"] == 12
    assert redacted["items"][0]["secret"] == "[REDACTED]"


def test_summarize_mcp_rows_groups_by_client_tool_and_error() -> None:
    rows = [
        {
            "client_name": "claude",
            "tool": "gpc.search",
            "success": True,
            "duration_ms": 10,
            "project_slug": "gpc",
        },
        {
            "client_name": "claude",
            "tool": "gpc.search",
            "success": False,
            "duration_ms": 30,
            "project_slug": "gpc",
        },
        {
            "client_name": "codex",
            "tool": "gpc.context",
            "success": True,
            "duration_ms": 20,
            "project_slug": "other",
        },
    ]

    summary = summarize_mcp_rows(rows)

    assert summary["total_calls"] == 3
    assert summary["by_client"][0]["client"] == "claude"
    assert summary["by_tool"][0]["tool"] == "gpc.search"
    assert summary["errors_by_tool"][0]["errors"] == 1
    assert summary["top_projects"][0]["project"] == "gpc"
