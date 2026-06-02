from __future__ import annotations

from gpc.health_report import build_health_report, render_health_markdown


def test_build_health_report_rolls_up_statuses_and_counts() -> None:
    report = build_health_report(
        project={"slug": "gpc", "name": "GPC"},
        index_status={
            "files": 10,
            "chunks": 20,
            "qdrant_points": 20,
            "runs": [{"status": "succeeded"}],
        },
        verification={
            "overall_status": "warn",
            "summary": {"pass": 3, "warn": 1, "fail": 0},
        },
        graph_summary={
            "found": True,
            "summary": {"node_count": 7, "community_count": 2},
        },
        mcp_usage={"total_calls": 5, "by_tool": [{"tool": "gpc_search", "calls": 3}]},
        drift_signals=[{"severity": "warn"}],
    )

    assert report.overall_status == "warn"
    assert report.sections["index"]["files"] == 10
    assert report.sections["graph"]["node_count"] == 7
    assert report.sections["drift"]["signals"] == 1


def test_render_health_markdown_includes_executive_sections() -> None:
    report = build_health_report(project={"slug": "gpc"})
    markdown = render_health_markdown(report)

    assert "# GPC Health Report" in markdown
    assert "## Index" in markdown
    assert "## Recommended Actions" in markdown
