from __future__ import annotations

from gpc.cross_project_audit import (
    build_cross_project_audit,
    render_cross_project_audit_markdown,
)


def test_build_cross_project_audit_flags_critical_gaps() -> None:
    audit = build_cross_project_audit(
        [
            {
                "slug": "alpha-project",
                "name": "Alpha",
                "root_path": "/workspace/.gpc/projects/alpha-project",
                "aliases": ["alpha"],
                "embedding_model": "nomic-embed-text:latest",
                "repos": [
                    {
                        "slug": "alpha-api",
                        "root_path": "/Users/me/Github/alpha/.worktrees/api",
                    },
                    {
                        "slug": "alpha-web",
                        "root_path": "/Users/me/Github/alpha/.worktrees/web",
                    },
                ],
                "graph_summary": {
                    "found": True,
                    "god_nodes": [
                        {"label": "AlphaService", "repo": "alpha-api", "degree": 11}
                    ],
                    "communities": [
                        {
                            "community": 1,
                            "node_count": 14,
                            "repo_count": 2,
                            "sample_labels": ["AlphaService", "AlphaController"],
                        }
                    ],
                    "cross_repo_bridges": [
                        {
                            "rule": "same_code_symbol",
                            "confidence": "INFERRED",
                            "count": 4,
                        }
                    ],
                },
            },
            {
                "slug": "beta_project",
                "name": "Beta",
                "root_path": "/Users/me/Github/beta",
                "embedding_model": "nomic-embed-text:latest",
                "repos": [],
                "graph_summary": {"found": False},
                "graph_note": "No Graphify projection found for this registered project.",
            },
            {
                "slug": "gamma-project",
                "name": "Gamma",
                "root_path": "/Users/me/Github/gamma",
                "embedding_model": "text-embedding-3-large",
                "repos": [{"slug": "gamma_api", "root_path": "/srv/gamma_api"}],
                "graph_summary": {
                    "found": True,
                    "god_nodes": [
                        {"label": "GammaService", "repo": "gamma_api", "degree": 7}
                    ],
                    "communities": [],
                    "cross_repo_bridges": [],
                },
            },
        ],
        audit_date="2026-09-17",
    )

    assert audit["projects_audited"] == 3
    assert any(
        "beta_project" in gap and "no Graphify projection" in gap
        for gap in audit["critical_gaps"]
    )
    assert any(
        "gamma-project" in gap and "text-embedding-3-large" in gap
        for gap in audit["critical_gaps"]
    )
    assert any(
        "beta_project" in gap and "kebab-case" in gap for gap in audit["observations"]
    )
    assert audit["overview"]["critical_gap_count"] == len(audit["critical_gaps"])
    assert any(
        project["slug"] == "gamma-project" and project["gaps"]
        for project in audit["projects"]
    )


def test_render_cross_project_audit_markdown_includes_expected_sections() -> None:
    audit = build_cross_project_audit(
        [
            {
                "slug": "gpc-local-verify",
                "name": "GPC Local Verify",
                "root_path": "/Users/me/Github/Global-Project-Context.worktrees/gpc-local-verify",
                "embedding_model": "nomic-embed-text:latest",
                "repos": [
                    {
                        "slug": "gpc-local-verify",
                        "root_path": "/Users/me/Github/Global-Project-Context.worktrees/gpc-local-verify",
                    }
                ],
                "graph_summary": {
                    "found": True,
                    "god_nodes": [
                        {
                            "label": "graph_summary()",
                            "repo": "gpc-local-verify",
                            "degree": 9,
                        }
                    ],
                    "communities": [
                        {
                            "community": 7,
                            "node_count": 21,
                            "repo_count": 1,
                            "sample_labels": ["graph_summary()", "collect_metrics()"],
                        }
                    ],
                    "cross_repo_bridges": [
                        {
                            "rule": "same_source_file",
                            "confidence": "INFERRED",
                            "count": 2,
                        }
                    ],
                },
            }
        ],
        audit_date="2026-09-17",
    )

    markdown = render_cross_project_audit_markdown(audit)

    assert "# GPC Cross-Project Audit — 2026-09-17" in markdown
    assert "## Project Overview" in markdown
    assert "## Convention Comparison" in markdown
    assert "## Per-Project Findings" in markdown
    assert "### gpc-local-verify" in markdown
    assert "#### God Nodes" in markdown
    assert "#### Communities" in markdown
    assert "#### Cross-Repo Bridges" in markdown
    assert "No critical gaps were triggered by the current heuristics." in markdown
