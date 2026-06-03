from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from psycopg.rows import dict_row

from gpc.db import pg_connection
from gpc.registry import resolve_project


@dataclass(frozen=True)
class MaintenanceFinding:
    name: str
    severity: str
    count: int
    message: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MaintenanceReport:
    project_slug: str
    dry_run: bool
    findings: list[MaintenanceFinding]

    @property
    def summary(self) -> dict[str, int]:
        summary = {"warn": 0, "fail": 0, "pass": 0}
        for finding in self.findings:
            summary[finding.severity] = summary.get(finding.severity, 0) + 1
        if not self.findings:
            summary["pass"] = 1
        return summary

    @property
    def requires_attention(self) -> bool:
        return any(
            finding.severity in {"warn", "fail"} and finding.count > 0
            for finding in self.findings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "dry_run": self.dry_run,
            "summary": self.summary,
            "requires_attention": self.requires_attention,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def build_maintenance_report(
    project_slug: str, findings: list[MaintenanceFinding]
) -> MaintenanceReport:
    return MaintenanceReport(project_slug=project_slug, dry_run=True, findings=findings)


def diagnose_project_maintenance(
    project: str, cwd: str | None = None
) -> MaintenanceReport:
    resolved = resolve_project(project=project, cwd=cwd)
    project_id = str(resolved["id"])
    findings: list[MaintenanceFinding] = []
    with pg_connection(row_factory=dict_row) as conn:
        chunks_without_files = conn.execute(
            """
            select count(*) as count
            from gpc_chunks c
            left join gpc_files f on f.id = c.file_id
            where c.project_id = %s and f.id is null
            """,
            (project_id,),
        ).fetchone()["count"]
        files_without_repo = conn.execute(
            """
            select count(*) as count
            from gpc_files f
            left join gpc_repos r on r.id = f.repo_id
            where f.project_id = %s and (f.repo_id is not null and r.id is null)
            """,
            (project_id,),
        ).fetchone()["count"]
        duplicate_aliases = conn.execute("""
            select count(*) as count
            from (
              select alias from gpc_project_aliases group by alias having count(*) > 1
            ) dup
            """).fetchone()["count"]
        stale_metrics = conn.execute("""
            select count(*) as count
            from gpc_self_metrics m
            left join gpc_projects p on p.slug = m.project_slug
            where p.id is null
            """).fetchone()["count"]
    _append_count(
        findings,
        "chunks_without_files",
        chunks_without_files,
        "Chunks without backing files.",
        "Re-run gpc-index; investigate if count remains non-zero.",
    )
    _append_count(
        findings,
        "files_without_repo",
        files_without_repo,
        "Files referencing missing repos.",
        "Check gpc_repos registry and reindex the affected project.",
    )
    _append_count(
        findings,
        "duplicate_aliases",
        duplicate_aliases,
        "Duplicate project aliases detected.",
        "Rename or remove duplicate aliases before relying on alias resolution.",
        severity="fail",
    )
    _append_count(
        findings,
        "stale_self_metrics",
        stale_metrics,
        "Self-metrics rows reference missing projects.",
        "Keep as diagnostic only; cleanup should be a separate confirmed operation.",
    )
    return build_maintenance_report(resolved["slug"], findings)


def _append_count(
    findings: list[MaintenanceFinding],
    name: str,
    count: int,
    message: str,
    remediation: str,
    *,
    severity: str = "warn",
) -> None:
    if count:
        findings.append(
            MaintenanceFinding(name, severity, int(count), message, remediation)
        )
