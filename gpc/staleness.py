from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

from gpc.config import POSTGRES_DSN
from gpc.indexer import SUPPORTED_EXTENSIONS, SUPPORTED_FILENAMES
from gpc.registry import resolve_project

IGNORED_PREFIXES = (".git/", "venv/", ".venv/", ".gpc/", "__pycache__/")
IGNORED_PARTS = ("/__pycache__/",)
GRAPHIFY_ALLOWED = "graphify-out/GRAPH_REPORT.md"


@dataclass(frozen=True)
class StalenessReport:
    project_slug: str | None
    missing_from_index: list[str]
    deleted_but_indexed: list[str]
    modified_since_index: list[str]
    graphify_report_stale: bool = False

    @property
    def is_stale(self) -> bool:
        return bool(
            self.missing_from_index
            or self.deleted_but_indexed
            or self.modified_since_index
            or self.graphify_report_stale
        )

    @property
    def summary(self) -> dict[str, int]:
        return {
            "missing_from_index": len(self.missing_from_index),
            "deleted_but_indexed": len(self.deleted_but_indexed),
            "modified_since_index": len(self.modified_since_index),
            "graphify_report_stale": int(self.graphify_report_stale),
        }

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["is_stale"] = self.is_stale
        data["summary"] = self.summary
        data["remediation"] = remediation_for_staleness(self)
        return data


def is_relevant_path(path: str) -> bool:
    normalized = path.strip().lstrip("./")
    if not normalized:
        return False
    if normalized.startswith("graphify-out/"):
        return normalized == GRAPHIFY_ALLOWED
    if any(normalized.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return False
    if any(part in normalized for part in IGNORED_PARTS):
        return False
    path_obj = Path(normalized)
    if (
        path_obj.suffix.lower() not in SUPPORTED_EXTENSIONS
        and path_obj.name not in SUPPORTED_FILENAMES
    ):
        return False
    return True


def analyze_staleness(
    *,
    tracked_files: Iterable[str],
    indexed_files: Iterable[str],
    modified_files: Iterable[str],
    project_slug: str | None = None,
    graphify_report_is_stale: bool = False,
) -> StalenessReport:
    tracked = {path for path in tracked_files if is_relevant_path(path)}
    indexed = {path for path in indexed_files if is_relevant_path(path)}
    modified = {path for path in modified_files if is_relevant_path(path)}
    return StalenessReport(
        project_slug=project_slug,
        missing_from_index=sorted(
            path for path in tracked - indexed if path != GRAPHIFY_ALLOWED
        ),
        deleted_but_indexed=sorted(indexed - tracked),
        modified_since_index=sorted(modified & indexed),
        graphify_report_stale=bool(graphify_report_is_stale),
    )


def detect_project_staleness(
    project: str | None = None, cwd: str | None = None
) -> StalenessReport:
    resolved = resolve_project(project=project, cwd=cwd)
    root = Path(resolved["root_path"]).expanduser().resolve(strict=False)
    tracked = _git_lines(root, ["ls-files", "--exclude-standard"])
    present = _git_lines(
        root, ["ls-files", "--cached", "--others", "--exclude-standard"]
    )
    indexed_info = _indexed_file_info(str(resolved["id"]))
    indexed = list(indexed_info)
    modified_paths = _modified_after_index(root, indexed_info)
    graph_stale = _graphify_report_is_stale(root, tracked)
    return analyze_staleness(
        project_slug=resolved["slug"],
        tracked_files=present,
        indexed_files=indexed,
        modified_files=modified_paths,
        graphify_report_is_stale=graph_stale,
    )


def remediation_for_staleness(report: StalenessReport) -> list[str]:
    remediation: list[str] = []
    if (
        report.missing_from_index
        or report.deleted_but_indexed
        or report.modified_since_index
    ):
        remediation.append(
            "Run gpc-index from the repository root after reviewing the working tree."
        )
    if report.graphify_report_stale:
        remediation.append(
            "Run graphify update ., then gpc graph-sync . --project <project> --repo <repo>."
        )
    return remediation


def _git_lines(root: Path, args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()


def _indexed_file_info(project_id: str) -> dict[str, object]:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        rows = conn.execute(
            "select relative_path, indexed_at from gpc_files where project_id = %s",
            (project_id,),
        ).fetchall()
    return {row["relative_path"]: row["indexed_at"] for row in rows}


def _modified_after_index(root: Path, indexed_info: dict[str, object]) -> list[str]:
    stale: list[str] = []
    for relative_path, indexed_at in indexed_info.items():
        if not is_relevant_path(relative_path):
            continue
        full_path = root / relative_path
        if not full_path.exists():
            continue
        try:
            indexed_ts = indexed_at.timestamp()  # type: ignore[union-attr]
        except AttributeError:
            continue
        if full_path.stat().st_mtime > indexed_ts:
            stale.append(relative_path)
    return stale


def _graphify_report_is_stale(root: Path, tracked: Iterable[str]) -> bool:
    if GRAPHIFY_ALLOWED not in set(tracked):
        return False
    report = root / GRAPHIFY_ALLOWED
    if not report.exists():
        return True
    report_mtime = report.stat().st_mtime
    for path in tracked:
        if not is_relevant_path(path) or path == GRAPHIFY_ALLOWED:
            continue
        full_path = root / path
        if full_path.suffix in {".py", ".js", ".ts", ".md"} and full_path.exists():
            if full_path.stat().st_mtime > report_mtime:
                return True
    return False
