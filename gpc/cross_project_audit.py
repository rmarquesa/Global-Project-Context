from __future__ import annotations

"""Cross-project audit report generation for registered GPC projects."""

from collections import Counter
from datetime import date
from pathlib import Path
import re
from typing import Any

from gpc.config import ROOT_DIR
from gpc.embeddings import active_embedding_model
from gpc.graph_query import graph_summary
from gpc.registry import list_projects, list_repos

EXPECTED_EMBEDDING_MODEL = "nomic-embed-text:latest"


def generate_cross_project_audit(
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Collect registered-project graph summaries and write a markdown audit."""

    audit = build_cross_project_audit(_collect_project_records(), audit_date=date.today())
    path = _resolve_output_path(output_path, audit["audit_date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(audit["markdown"], encoding="utf-8")
    audit["output_path"] = str(path)
    return audit


def build_cross_project_audit(
    project_records: list[dict[str, Any]],
    *,
    audit_date: date | str | None = None,
) -> dict[str, Any]:
    """Build a structured cross-project audit from already-collected project data."""

    normalized_date = (
        audit_date.isoformat() if isinstance(audit_date, date) else audit_date
    ) or date.today().isoformat()

    prepared_projects = [_prepare_project_record(record) for record in project_records]
    overview = _build_overview(prepared_projects)
    critical_gaps, observations = _collect_gaps(prepared_projects)
    overview["critical_gap_count"] = len(critical_gaps)
    comparison = [_comparison_row(project) for project in prepared_projects]

    result = {
        "audit_date": normalized_date,
        "projects_audited": len(prepared_projects),
        "projects": prepared_projects,
        "overview": overview,
        "convention_comparison": comparison,
        "critical_gaps": critical_gaps,
        "observations": observations,
        "heuristics": [
            "critical: registered project missing Graphify projection (graph_summary found=false or errored)",
            "critical: project embedding model differs from the majority embedding model across audited projects",
            f"critical: majority embedding model differs from expected {EXPECTED_EMBEDDING_MODEL}",
            "warning: project or repo slugs diverge from kebab-case naming",
            "warning: project has no registered repos or mixes repo root path structures",
        ],
    }
    result["markdown"] = render_cross_project_audit_markdown(result)
    return result


def render_cross_project_audit_markdown(audit: dict[str, Any]) -> str:
    """Render a human-readable markdown report for filing follow-up issues."""

    overview = audit["overview"]
    projects = audit["projects"]
    critical_gaps = audit["critical_gaps"]
    observations = audit["observations"]
    expected = audit["overview"].get("expected_embedding_model", EXPECTED_EMBEDDING_MODEL)

    lines = [
        f"# GPC Cross-Project Audit — {audit['audit_date']}",
        "",
        "This audit aggregates `gpc_graph_summary` across every registered GPC project "
        "and compares naming, root-path structure and embedding-model conventions.",
        "",
        "## Executive Summary",
        "",
        (
            f"{overview['registered_projects']} registered project(s) were audited; "
            f"{overview['graph_projects_found']} currently expose a Graphify projection and "
            f"{overview['critical_gap_count']} critical gap(s) were flagged."
        ),
        "",
        (
            f"The current majority embedding model is `{overview['majority_embedding_model'] or 'unknown'}`; "
            f"the expected shared convention is `{expected}`."
        ),
        "",
        "## Project Overview",
        "",
        "| Project | Repos | Graphify | God nodes | Communities | Embedding model | Root path |",
        "|---|---:|---|---:|---:|---|---|",
    ]
    for project in projects:
        lines.append(
            "| {slug} | {repo_count} | {graphify} | {god_nodes} | {communities} | `{embedding}` | {root_style} |".format(
                slug=project["slug"],
                repo_count=project["repo_count"],
                graphify="yes" if project["graph_found"] else "no",
                god_nodes=len(project["god_nodes"]),
                communities=project["community_count"],
                embedding=project["embedding_model"],
                root_style=project["project_root_style"],
            )
        )

    lines.extend(
        [
            "",
            "## Convention Comparison",
            "",
            "| Project | Project slug | Repo slug convention | Repo root structure | Embedding model | Notes |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in audit["convention_comparison"]:
        lines.append(
            "| {project} | {project_slug_style} | {repo_slug_style} | {repo_root_style} | `{embedding_model}` | {notes} |".format(
                **row
            )
        )

    lines.extend(["", "## Per-Project Findings", ""])
    for project in projects:
        lines.extend(
            [
                f"### {project['slug']}",
                "",
                f"- Display name: {project['name'] or project['slug']}",
                f"- Project root: `{project['root_path']}` ({project['project_root_style']})",
                f"- Registered repos: {project['repo_count']}",
                f"- Embedding model: `{project['embedding_model']}`",
                f"- Graphify projection: {'present' if project['graph_found'] else 'missing'}",
            ]
        )
        if project.get("graph_note"):
            lines.append(f"- Graph note: {project['graph_note']}")
        if project["repo_slugs"]:
            lines.append(f"- Repo slugs: {', '.join(f'`{slug}`' for slug in project['repo_slugs'])}")
        lines.extend(["", "#### God Nodes", ""])
        if project["god_nodes"]:
            lines.extend([
                "| Label | Repo | Degree |",
                "|---|---|---:|",
            ])
            for node in project["god_nodes"]:
                lines.append(
                    f"| `{node.get('label', 'unknown')}` | {node.get('repo', 'unknown')} | {node.get('degree', 0)} |"
                )
        else:
            lines.append("No graph summary available for god-node analysis.")

        lines.extend(["", "#### Communities", ""])
        if project["communities"]:
            lines.extend([
                "| Community | Nodes | Repos | Sample labels |",
                "|---|---:|---:|---|",
            ])
            for community in project["communities"]:
                labels = ", ".join(
                    f"`{label}`" for label in (community.get("sample_labels") or [])
                ) or "—"
                lines.append(
                    f"| {community.get('community', 'n/a')} | {community.get('node_count', 0)} | {community.get('repo_count', 0)} | {labels} |"
                )
        else:
            lines.append("No community data available.")

        if project["cross_repo_bridges"]:
            lines.extend(["", "#### Cross-Repo Bridges", "", "| Rule | Confidence | Count |", "|---|---|---:|"])
            for bridge in project["cross_repo_bridges"]:
                lines.append(
                    f"| {bridge.get('rule', 'unknown')} | {bridge.get('confidence', 'unknown')} | {bridge.get('count', 0)} |"
                )

        if project["gaps"]:
            lines.extend(["", "#### Project Gaps", ""])
            lines.extend(f"- {gap}" for gap in project["gaps"])

        lines.append("")

    lines.extend(["## Flagged Gaps / Inconsistencies", ""])
    if critical_gaps:
        lines.append("### Critical gaps")
        lines.append("")
        lines.extend(f"- {gap}" for gap in critical_gaps)
        lines.append("")
    else:
        lines.extend(["No critical gaps were triggered by the current heuristics.", ""])

    if observations:
        lines.append("### Additional observations")
        lines.append("")
        lines.extend(f"- {gap}" for gap in observations)
        lines.append("")

    lines.extend(["## Heuristics Used", ""])
    lines.extend(f"- {rule}" for rule in audit.get("heuristics", []))
    lines.append("")
    return "\n".join(lines)


def _collect_project_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for project in list_projects():
        slug = str(project.get("slug") or "")
        repos = list_repos(slug)
        model = active_embedding_model()
        note = None
        try:
            summary = graph_summary(slug, top_k_gods=8, include_cohesion=True)
        except Exception as exc:  # noqa: BLE001 - audit should degrade gracefully
            summary = {"project_slug": slug, "found": False, "error": str(exc)}
            note = f"graph_summary errored: {exc}"
        if not summary.get("found") and not note:
            note = "No Graphify projection found for this registered project."
        records.append(
            {
                "slug": slug,
                "name": project.get("name"),
                "root_path": str(project.get("root_path") or ""),
                "aliases": list(project.get("aliases") or []),
                "embedding_model": model,
                "repos": [dict(repo) for repo in repos],
                "graph_summary": summary,
                "graph_note": note,
            }
        )
    return records


def _prepare_project_record(record: dict[str, Any]) -> dict[str, Any]:
    slug = str(record.get("slug") or "unknown")
    repos = [dict(repo) for repo in record.get("repos", [])]
    repo_slugs = [str(repo.get("slug") or "") for repo in repos if repo.get("slug")]
    repo_root_styles = [
        _classify_root_path(str(repo.get("root_path") or "")) for repo in repos
    ]
    graph = record.get("graph_summary") or {}
    god_nodes = list(graph.get("god_nodes") or [])[:8]
    communities = list(graph.get("communities") or [])
    cross_repo_bridges = list(graph.get("cross_repo_bridges") or [])

    return {
        "slug": slug,
        "name": record.get("name"),
        "root_path": str(record.get("root_path") or ""),
        "aliases": list(record.get("aliases") or []),
        "project_slug_style": _classify_slug(slug),
        "project_root_style": _classify_root_path(str(record.get("root_path") or "")),
        "embedding_model": str(record.get("embedding_model") or "unknown"),
        "repos": repos,
        "repo_count": len(repos),
        "repo_slugs": repo_slugs,
        "repo_slug_style": _summarize_slug_styles(repo_slugs),
        "repo_root_style": _summarize_root_styles(repo_root_styles),
        "graph_found": bool(graph.get("found")),
        "graph_note": record.get("graph_note") or graph.get("error"),
        "god_nodes": god_nodes,
        "communities": communities,
        "community_count": int(graph.get("community_count") or len(communities)),
        "cross_repo_bridges": cross_repo_bridges,
        "gaps": [],
    }


def _build_overview(projects: list[dict[str, Any]]) -> dict[str, Any]:
    models = [
        project["embedding_model"] for project in projects if project.get("embedding_model")
    ]
    majority = Counter(models).most_common(1)[0][0] if models else None
    return {
        "registered_projects": len(projects),
        "graph_projects_found": sum(1 for project in projects if project["graph_found"]),
        "projects_without_graph": sum(1 for project in projects if not project["graph_found"]),
        "repo_count": sum(project["repo_count"] for project in projects),
        "majority_embedding_model": majority,
        "expected_embedding_model": EXPECTED_EMBEDDING_MODEL,
        "critical_gap_count": 0,
    }


def _collect_gaps(projects: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    critical: list[str] = []
    observations: list[str] = []
    models = [
        project["embedding_model"] for project in projects if project.get("embedding_model")
    ]
    majority = Counter(models).most_common(1)[0][0] if models else None

    if majority and majority != EXPECTED_EMBEDDING_MODEL:
        critical.append(
            f"Majority embedding model is `{majority}`, but roadmap convention expects `{EXPECTED_EMBEDDING_MODEL}`."
        )

    for project in projects:
        if not project["graph_found"]:
            critical.append(
                f"Project `{project['slug']}` is registered but has no Graphify projection; run Graphify before relying on cross-project audits."
            )
            project["gaps"].append("Missing Graphify projection.")

        if majority and project["embedding_model"] != majority:
            critical.append(
                f"Project `{project['slug']}` uses embedding model `{project['embedding_model']}` while the audited majority uses `{majority}`."
            )
            project["gaps"].append(
                f"Embedding model differs from majority (`{majority}`)."
            )

        if project["project_slug_style"] != "kebab-case":
            observations.append(
                f"Project `{project['slug']}` uses `{project['project_slug_style']}` instead of kebab-case naming."
            )
            project["gaps"].append("Project slug diverges from kebab-case.")

        if project["repo_slug_style"].startswith("mixed") or project["repo_slug_style"] not in {
            "kebab-case",
            "none",
        }:
            observations.append(
                f"Project `{project['slug']}` has repo slug convention `{project['repo_slug_style']}`."
            )
            project["gaps"].append(
                f"Repo slug convention is `{project['repo_slug_style']}`."
            )

        if project["repo_count"] == 0:
            observations.append(
                f"Project `{project['slug']}` has no registered repos; root-path comparison is incomplete."
            )
            project["gaps"].append("No repos registered under the project.")
        elif project["repo_root_style"].startswith("mixed"):
            observations.append(
                f"Project `{project['slug']}` mixes repo root-path structures: {project['repo_root_style']}."
            )
            project["gaps"].append("Repo roots mix structural conventions.")

    return critical, observations


def _comparison_row(project: dict[str, Any]) -> dict[str, Any]:
    notes: list[str] = []
    if project["aliases"]:
        notes.append(f"aliases={len(project['aliases'])}")
    if project.get("graph_note"):
        notes.append(project["graph_note"])
    if not notes:
        notes.append("aligned with current registry shape")
    return {
        "project": project["slug"],
        "project_slug_style": project["project_slug_style"],
        "repo_slug_style": project["repo_slug_style"],
        "repo_root_style": project["repo_root_style"],
        "embedding_model": project["embedding_model"],
        "notes": "; ".join(notes),
    }


def _resolve_output_path(output_path: str | Path | None, audit_date: str) -> Path:
    if output_path is None:
        return ROOT_DIR / "docs" / f"audit-{audit_date}.md"
    return Path(output_path).expanduser().resolve(strict=False)


def _classify_slug(value: str) -> str:
    if not value:
        return "missing"
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        return "kebab-case"
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value):
        return "snake_case"
    if re.fullmatch(r"[a-z][A-Za-z0-9]*", value) and any(ch.isupper() for ch in value):
        return "camelCase/mixed"
    return "other"


def _summarize_slug_styles(values: list[str]) -> str:
    if not values:
        return "none"
    styles = Counter(_classify_slug(value) for value in values)
    if len(styles) == 1:
        return next(iter(styles))
    return "mixed(" + ", ".join(f"{style}={count}" for style, count in sorted(styles.items())) + ")"


def _classify_root_path(value: str) -> str:
    if not value:
        return "missing"
    normalized = value.replace('\\', '/')
    if '/.gpc/projects/' in normalized:
        return 'synthetic-project-root'
    if '/.worktrees/' in normalized:
        return 'git-worktree'
    if '/Github/' in normalized or '/GitHub/' in normalized:
        return 'workspace-checkout'
    if normalized.startswith('/'):
        return 'absolute-path'
    return 'relative-or-other'


def _summarize_root_styles(values: list[str]) -> str:
    if not values:
        return 'none'
    styles = Counter(values)
    if len(styles) == 1:
        return next(iter(styles))
    return 'mixed(' + ', '.join(f'{style}={count}' for style, count in sorted(styles.items())) + ')'
