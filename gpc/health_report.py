from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from gpc.drift import list_drift_signals
from gpc.graph_query import graph_summary
from gpc.registry import resolve_project
from gpc.status import get_index_status
from gpc.verify import verify_project


@dataclass(frozen=True)
class HealthReport:
    project_slug: str
    overall_status: str
    sections: dict[str, Any]
    recommended_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_live_health_report(project: str, cwd: str | None = None) -> HealthReport:
    resolved = resolve_project(project=project, cwd=cwd)
    verification = verify_project(
        project=resolved["slug"], cwd=cwd, quick=True, live=False
    ).to_dict()
    try:
        graph = graph_summary(
            project_slug=resolved["slug"], top_k_gods=5, include_cohesion=True
        )
    except Exception as exc:
        graph = {"found": False, "error": str(exc)}
    try:
        from gpc.mcp_observability import summarize_mcp_usage

        mcp_usage = summarize_mcp_usage(project=resolved["slug"], window_hours=24)
    except Exception as exc:
        mcp_usage = {"error": str(exc), "total_calls": 0}
    try:
        drift = list_drift_signals(project=resolved["slug"], limit=10)
    except Exception:
        drift = []
    return build_health_report(
        project=resolved,
        index_status=get_index_status(project=resolved["slug"], cwd=cwd, runs=3),
        verification=verification,
        graph_summary=graph,
        mcp_usage=mcp_usage,
        drift_signals=drift,
    )


def build_health_report(
    *,
    project: dict[str, Any],
    index_status: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    graph_summary: dict[str, Any] | None = None,
    mcp_usage: dict[str, Any] | None = None,
    drift_signals: list[dict[str, Any]] | None = None,
) -> HealthReport:
    project_slug = project.get("slug") or "unknown"
    index_status = index_status or {}
    verification = verification or {}
    graph_summary = graph_summary or {}
    mcp_usage = mcp_usage or {}
    drift_signals = drift_signals or []
    sections = {
        "project": {"slug": project_slug, "name": project.get("name")},
        "index": {
            "files": index_status.get("files", 0),
            "chunks": index_status.get("chunks", 0),
            "qdrant_points": index_status.get("qdrant_points", 0),
            "latest_run_status": _latest_run_status(index_status),
        },
        "verification": {
            "overall_status": verification.get("overall_status", "unknown"),
            "summary": verification.get("summary", {}),
        },
        "graph": _graph_section(graph_summary),
        "mcp_usage": {
            "total_calls": mcp_usage.get("total_calls", 0),
            "by_tool": mcp_usage.get("by_tool", [])[:5],
            "by_client": mcp_usage.get("by_client", [])[:5],
        },
        "drift": {
            "signals": len(drift_signals),
            "warn_or_fail": sum(
                1
                for signal in drift_signals
                if signal.get("severity") in {"warn", "fail", "critical"}
            ),
        },
    }
    actions = _recommended_actions(sections)
    return HealthReport(
        project_slug=project_slug,
        overall_status=_overall_status(sections),
        sections=sections,
        recommended_actions=actions,
    )


def render_health_markdown(report: HealthReport) -> str:
    index = report.sections["index"]
    graph = report.sections["graph"]
    usage = report.sections["mcp_usage"]
    drift = report.sections["drift"]
    actions = report.recommended_actions or ["No immediate action."]
    lines = [
        "# GPC Health Report",
        "",
        f"Project: {report.project_slug}",
        f"Status: {report.overall_status}",
        "",
        "## Index",
        f"- files: {index['files']}",
        f"- chunks: {index['chunks']}",
        f"- qdrant_points: {index['qdrant_points']}",
        f"- latest_run_status: {index['latest_run_status']}",
        "",
        "## Graph",
        f"- found: {graph.get('found')}",
        f"- node_count: {graph.get('node_count', 0)}",
        f"- community_count: {graph.get('community_count', 0)}",
        "",
        "## MCP Usage",
        f"- total_calls_24h: {usage['total_calls']}",
        "",
        "## Drift",
        f"- signals: {drift['signals']}",
        f"- warn_or_fail: {drift['warn_or_fail']}",
        "",
        "## Recommended Actions",
        *[f"- {action}" for action in actions],
    ]
    return "\n".join(lines) + "\n"


def _latest_run_status(index_status: dict[str, Any]) -> str:
    runs = index_status.get("runs") or []
    if not runs:
        return "missing"
    return runs[0].get("status", "unknown")


def _graph_section(summary: dict[str, Any]) -> dict[str, Any]:
    nested = (
        summary.get("summary") if isinstance(summary.get("summary"), dict) else summary
    )
    return {
        "found": bool(summary.get("found")),
        "node_count": nested.get("node_count", 0),
        "community_count": nested.get("community_count", 0),
    }


def _overall_status(sections: dict[str, Any]) -> str:
    verification_status = sections["verification"].get("overall_status")
    if verification_status == "fail" or sections["index"]["files"] == 0:
        return "fail"
    if verification_status == "warn" or sections["drift"]["warn_or_fail"]:
        return "warn"
    return "pass"


def _recommended_actions(sections: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if sections["index"]["files"] == 0:
        actions.append("Run gpc-index for this project.")
    if not sections["graph"].get("found"):
        actions.append("Run graphify update . and gpc graph-sync for this repo.")
    if sections["verification"].get("overall_status") in {"warn", "fail"}:
        actions.append(
            "Run gpc verify --project <project> --json and inspect failing checks."
        )
    if sections["drift"].get("warn_or_fail"):
        actions.append("Inspect gpc_drift_signals before refactoring graph-heavy code.")
    return actions
