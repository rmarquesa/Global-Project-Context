"""Graph-derived test coverage portfolio (Fase 3.4).

Two topology-driven checks, matching ``docs/roadmap.md`` 3.4 verbatim:

* **External call coverage** — for every high-degree ("god") node with a
  ``calls``, ``imports``, or ``imports_from`` edge to a Graphify ``external``
  concept node (a stdlib/third-party symbol), does at least one file under
  the configured test roots reference the calling module? This is a coarse
  proxy for "is this call path exercised by a test", not a real coverage
  trace.
* **Rationale integrity** — for every ``rationale_for`` edge marked
  ``EXTRACTED`` (a docstring/comment Graphify attached to a symbol), does the
  target implementation node still exist in the graph? A dangling edge
  means the rationale outlived the code it described.

Discrepancies are returned as a report and, optionally, persisted to
``gpc_drift_signals`` (severity ``info``) so they surface next to other
drift signals — never as an automatic code or documentation change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from gpc.db import pg_connection
from gpc.graph import neo4j_driver

DEFAULT_TEST_ROOTS = ("tests",)
SIGNAL_TYPE = "graph_test_portfolio_gap"


@dataclass(frozen=True)
class PortfolioGap:
    check: str
    label: str
    source_file: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "label": self.label,
            "source_file": self.source_file,
            "detail": self.detail,
        }


def generate_test_portfolio_report(
    project_slug: str,
    *,
    test_roots: tuple[str, ...] = DEFAULT_TEST_ROOTS,
    repo_root: str | Path | None = None,
    min_god_node_degree: int = 10,
    persist: bool = False,
) -> dict[str, Any]:
    if not project_slug:
        raise ValueError("project_slug is required")

    root = Path(repo_root) if repo_root else Path.cwd()
    test_text = _load_test_corpus(root, test_roots)

    with neo4j_driver() as driver:
        with driver.session() as session:
            project = session.run(
                "MATCH (p:GraphifyProject {slug: $slug}) RETURN p.slug AS slug",
                slug=project_slug,
            ).single()
            if not project:
                return {"project_slug": project_slug, "found": False}

            external_calls = session.run(
                """
                MATCH (caller:GraphifyNode {project_slug: $slug})
                WITH caller, size([(caller)--() | 1]) AS degree
                WHERE degree >= $min_degree
                MATCH (caller)
                      -[r:GRAPHIFY_RELATION]->
                      (target:GraphifyNode {project_slug: $slug})
                WHERE r.relation IN ['calls', 'imports', 'imports_from']
                  AND (target.external = true OR target.node_type = 'external')
                RETURN DISTINCT caller.label AS label,
                       caller.source_file AS source_file,
                       target.label AS external_symbol,
                       r.relation AS relation
                ORDER BY label
                """,
                slug=project_slug,
                min_degree=min_god_node_degree,
            ).data()

            dangling_rationales = session.run(
                """
                MATCH (rationale:GraphifyNode {project_slug: $slug,
                                                file_type: 'rationale'})
                      -[r:GRAPHIFY_RELATION {relation: 'rationale_for'}]->
                      (target)
                WHERE r.confidence = 'EXTRACTED'
                WITH rationale, target
                WHERE target IS NULL OR NOT target:GraphifyNode
                RETURN rationale.label AS label,
                       rationale.source_file AS source_file
                ORDER BY label
                """,
                slug=project_slug,
            ).data()

            rationale_total = session.run(
                """
                MATCH (:GraphifyNode {project_slug: $slug, file_type: 'rationale'})
                      -[r:GRAPHIFY_RELATION {relation: 'rationale_for'}]->()
                WHERE r.confidence = 'EXTRACTED'
                RETURN count(*) AS total
                """,
                slug=project_slug,
            ).single()

    gaps: list[PortfolioGap] = []
    covered_calls = 0
    checked_calls = 0
    for row in external_calls:
        source_file = row.get("source_file") or ""
        if _is_under_test_roots(source_file, test_roots):
            # A test file importing something external is not itself a
            # coverage gap — it is the coverage.
            continue
        checked_calls += 1
        module_ref = _module_reference(source_file)
        if module_ref and module_ref in test_text:
            covered_calls += 1
            continue
        gaps.append(
            PortfolioGap(
                check="external_call_coverage",
                label=str(row.get("label")),
                source_file=row.get("source_file"),
                detail=(
                    f"No file under {', '.join(test_roots)} references "
                    f"`{module_ref or row.get('source_file')}`, which "
                    f"{row.get('relation', 'calls')}s external symbol "
                    f"`{row.get('external_symbol')}`."
                ),
            )
        )

    for row in dangling_rationales:
        gaps.append(
            PortfolioGap(
                check="rationale_for_integrity",
                label=str(row.get("label")),
                source_file=row.get("source_file"),
                detail="rationale_for edge marked EXTRACTED has no resolvable target node.",
            )
        )

    report = {
        "project_slug": project_slug,
        "found": True,
        "external_calls_checked": checked_calls,
        "external_calls_covered": covered_calls,
        "rationale_for_checked": (rationale_total or {}).get("total", 0),
        "rationale_for_dangling": len(dangling_rationales),
        "gaps": [gap.as_dict() for gap in gaps],
    }

    if persist:
        report["persisted"] = _persist_gaps(project_slug, gaps)

    return report


def _load_test_corpus(root: Path, test_roots: tuple[str, ...]) -> str:
    chunks: list[str] = []
    for test_root in test_roots:
        base = root / test_root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(chunks)


def _module_reference(source_file: str | None) -> str | None:
    if not source_file or not source_file.endswith(".py"):
        return None
    return source_file[: -len(".py")].replace("/", ".")


def _is_under_test_roots(source_file: str, test_roots: tuple[str, ...]) -> bool:
    normalized = source_file.replace("\\", "/").lstrip("/")
    return any(
        normalized == root or normalized.startswith(f"{root}/") for root in test_roots
    )


def _persist_gaps(project_slug: str, gaps: list[PortfolioGap]) -> int:
    if not gaps:
        return 0
    with pg_connection() as conn:
        project_row = conn.execute(
            "select id from gpc_projects where slug = %s",
            (project_slug,),
        ).fetchone()
        project_id = project_row[0] if project_row else None
        for gap in gaps:
            conn.execute(
                """
                insert into gpc_drift_signals (
                    project_id, project_slug, severity, signal_type, message,
                    evidence
                )
                values (%s, %s, 'info', %s, %s, %s)
                """,
                (
                    project_id,
                    project_slug,
                    SIGNAL_TYPE,
                    f"[{gap.check}] {gap.label}",
                    Jsonb(gap.as_dict()),
                ),
            )
    return len(gaps)
