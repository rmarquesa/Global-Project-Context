from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg
from qdrant_client import QdrantClient

from gpc.config import (
    COLLECTION_NAME,
    OLLAMA_EMBEDDING_MODEL,
    OLLAMA_HOST,
    POSTGRES_DSN,
    QDRANT_HOST,
    QDRANT_PORT,
)
from gpc.graph import neo4j_driver
from gpc.graph_query import graph_summary
from gpc.registry import resolve_project
from gpc.status import get_index_status
from gpc.staleness import detect_project_staleness

CheckStatus = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: CheckStatus
    message: str
    remediation: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class VerificationReport:
    project_slug: str | None
    checks: list[VerificationCheck]

    @property
    def summary(self) -> dict[str, int]:
        return summarize_checks(self.checks)["summary"]

    @property
    def overall_status(self) -> CheckStatus:
        return summarize_checks(self.checks)["overall_status"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
        }


def summarize_checks(checks: Iterable[VerificationCheck]) -> dict[str, Any]:
    summary = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in checks:
        summary[check.status] += 1
    if summary["fail"]:
        overall: CheckStatus = "fail"
    elif summary["warn"]:
        overall = "warn"
    elif summary["pass"]:
        overall = "pass"
    else:
        overall = "skip"
    return {"summary": summary, "overall_status": overall}


def verify_project_path(root_path: str | Path) -> VerificationCheck:
    root = Path(root_path).expanduser().resolve(strict=False)
    if root.exists() and root.is_dir():
        return VerificationCheck("project_path", "pass", f"Project path exists: {root}")
    return VerificationCheck(
        "project_path",
        "fail",
        f"Project path does not exist or is not a directory: {root}",
        remediation="Pass a valid --cwd or register the project again with gpc init.",
    )


def verify_graphify_report_file(root_path: str | Path) -> VerificationCheck:
    root = Path(root_path).expanduser().resolve(strict=False)
    graphify_dir = root / "graphify-out"
    report = graphify_dir / "GRAPH_REPORT.md"
    if report.exists() and report.is_file():
        return VerificationCheck(
            "graphify_report", "pass", f"Graphify report exists: {report}"
        )
    if graphify_dir.exists():
        return VerificationCheck(
            "graphify_report",
            "warn",
            "graphify-out exists but GRAPH_REPORT.md is missing.",
            remediation="Run graphify update . from the repository root.",
        )
    return VerificationCheck(
        "graphify_report",
        "skip",
        "graphify-out is absent; Graphify may not be enabled for this repository.",
    )


def verify_project_resolution(
    project: str | None = None, cwd: str | None = None
) -> tuple[dict[str, Any] | None, VerificationCheck]:
    try:
        resolved = resolve_project(project=project, cwd=cwd)
    except Exception as exc:
        return None, VerificationCheck(
            "project_resolution",
            "fail",
            f"Could not resolve project: {exc}",
            remediation="Run gpc init . --project <slug> --repo <repo> or pass --project explicitly.",
        )
    return resolved, VerificationCheck(
        "project_resolution",
        "pass",
        f"Resolved project {resolved['slug']} ({resolved['id']}).",
        details={"slug": resolved["slug"], "root_path": resolved.get("root_path")},
    )


def verify_index_state(
    project: str | None = None, cwd: str | None = None
) -> VerificationCheck:
    try:
        status = get_index_status(project=project, cwd=cwd, runs=1)
    except Exception as exc:
        return VerificationCheck(
            "index_state",
            "fail",
            f"Could not read index state: {exc}",
            remediation="Check Postgres/Qdrant services and run gpc-index after fixing connectivity.",
        )

    runs = status.get("runs") or []
    files = int(status.get("files") or 0)
    chunks = int(status.get("chunks") or 0)
    points = int(status.get("qdrant_points") or 0)
    if not runs:
        return VerificationCheck(
            "index_state",
            "warn",
            "No index runs found for this project.",
            remediation="Run gpc-index . --slug <project> from the repository root.",
            details={"files": files, "chunks": chunks, "qdrant_points": points},
        )
    latest = runs[0]
    if latest.get("status") != "succeeded":
        return VerificationCheck(
            "index_state",
            "fail",
            f"Latest index run status is {latest.get('status')!r}.",
            remediation="Inspect the latest run error and rerun gpc-index after fixing it.",
            details={"latest_run": latest},
        )
    if files == 0 or chunks == 0 or points == 0:
        return VerificationCheck(
            "index_state",
            "warn",
            "Index exists but has zero files, chunks, or Qdrant points.",
            remediation="Run gpc-index . --slug <project> and verify filters are not excluding useful files.",
            details={"files": files, "chunks": chunks, "qdrant_points": points},
        )
    return VerificationCheck(
        "index_state",
        "pass",
        f"Indexed context is populated: files={files}, chunks={chunks}, qdrant_points={points}.",
        details={"files": files, "chunks": chunks, "qdrant_points": points},
    )


def verify_postgres() -> VerificationCheck:
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute("select 1").fetchone()
    except Exception as exc:
        return VerificationCheck(
            "postgres",
            "fail",
            f"Postgres is unreachable: {exc}",
            remediation="Start the local Postgres service from docker-compose.yaml and verify GPC_POSTGRES_DSN.",
        )
    return VerificationCheck("postgres", "pass", "Postgres is reachable.")


def verify_qdrant() -> VerificationCheck:
    try:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        client.get_collection(COLLECTION_NAME)
    except Exception as exc:
        return VerificationCheck(
            "qdrant",
            "fail",
            f"Qdrant collection is unreachable: {exc}",
            remediation="Start Qdrant and run gpc init-qdrant if the collection is missing.",
        )
    return VerificationCheck(
        "qdrant", "pass", f"Qdrant collection {COLLECTION_NAME!r} is reachable."
    )


def verify_ollama() -> VerificationCheck:
    url = f"{OLLAMA_HOST}/api/tags"
    try:
        request = Request(url, method="GET")
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        return VerificationCheck(
            "ollama",
            "fail",
            f"Ollama is unreachable: {exc}",
            remediation="Start Ollama and pull the configured embedding model.",
        )
    if OLLAMA_EMBEDDING_MODEL not in body:
        return VerificationCheck(
            "ollama",
            "warn",
            f"Ollama is reachable but model {OLLAMA_EMBEDDING_MODEL!r} was not listed.",
            remediation=f"Run ollama pull {OLLAMA_EMBEDDING_MODEL}.",
        )
    return VerificationCheck(
        "ollama", "pass", f"Ollama model {OLLAMA_EMBEDDING_MODEL!r} is available."
    )


def verify_neo4j() -> VerificationCheck:
    try:
        with neo4j_driver() as driver:
            driver.verify_connectivity()
    except Exception as exc:
        return VerificationCheck(
            "neo4j",
            "fail",
            f"Neo4j is unreachable: {exc}",
            remediation="Start Neo4j from docker-compose.yaml and verify GPC_NEO4J_* settings.",
        )
    return VerificationCheck("neo4j", "pass", "Neo4j is reachable.")


def verify_graph_summary(project_slug: str) -> VerificationCheck:
    try:
        summary = graph_summary(project_slug, top_k_gods=3, include_cohesion=False)
    except Exception as exc:
        return VerificationCheck(
            "graph_summary",
            "fail",
            f"Could not query graph summary: {exc}",
            remediation="Check Neo4j connectivity and Graphify projection state.",
        )
    if not summary.get("found"):
        return VerificationCheck(
            "graph_summary",
            "warn",
            f"No GraphifyProject found for project {project_slug!r}.",
            remediation="Run graphify update ., then gpc graph-sync . --project <project> --repo <repo>.",
        )
    repos = summary.get("repos") or []
    nodes = sum(int(repo.get("node_count") or 0) for repo in repos)
    return VerificationCheck(
        "graph_summary",
        "pass" if nodes else "warn",
        f"Graph summary found with {nodes} nodes across {len(repos)} repo(s).",
        remediation=(
            None
            if nodes
            else "Run graphify update . and gpc graph-sync . --project <project> --repo <repo>."
        ),
        details={"repos": repos},
    )


def verify_staleness(
    project: str | None = None, cwd: str | None = None
) -> VerificationCheck:
    try:
        report = detect_project_staleness(project=project, cwd=cwd)
    except Exception as exc:
        return VerificationCheck(
            "staleness",
            "warn",
            f"Could not compute staleness: {exc}",
            remediation="Run git status and gpc-index manually to verify context freshness.",
        )
    if not report.is_stale:
        return VerificationCheck(
            "staleness", "pass", "Indexed context appears fresh against Git state."
        )
    return VerificationCheck(
        "staleness",
        "warn",
        "Indexed context may be stale against Git state.",
        remediation="; ".join(report.to_dict()["remediation"]),
        details=report.to_dict(),
    )


def verify_project(
    *,
    project: str | None = None,
    cwd: str | None = None,
    quick: bool = False,
    live: bool = False,
) -> VerificationReport:
    resolved, resolution_check = verify_project_resolution(project=project, cwd=cwd)
    checks = [resolution_check]
    if resolved:
        checks.append(verify_project_path(resolved["root_path"]))
        checks.append(verify_graphify_report_file(resolved["root_path"]))
        checks.append(verify_index_state(project=resolved["slug"], cwd=cwd))
        checks.append(verify_staleness(project=resolved["slug"], cwd=cwd))
    if quick:
        checks.extend(
            [
                VerificationCheck("postgres", "skip", "Skipped in quick mode."),
                VerificationCheck("qdrant", "skip", "Skipped in quick mode."),
                VerificationCheck("ollama", "skip", "Skipped in quick mode."),
                VerificationCheck("neo4j", "skip", "Skipped in quick mode."),
                VerificationCheck("graph_summary", "skip", "Skipped in quick mode."),
            ]
        )
    elif live or resolved:
        checks.extend(
            [verify_postgres(), verify_qdrant(), verify_ollama(), verify_neo4j()]
        )
        if resolved:
            checks.append(verify_graph_summary(resolved["slug"]))
    return VerificationReport(
        project_slug=resolved["slug"] if resolved else project, checks=checks
    )
