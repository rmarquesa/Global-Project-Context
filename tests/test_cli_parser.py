from __future__ import annotations

from gpc.cli import build_parser


def parse_args(argv: list[str]):
    return build_parser().parse_args(argv)


def test_verify_parser_accepts_quick_json_project() -> None:
    args = parse_args(["verify", "--project", "gpc", "--quick", "--json"])

    assert args.project == "gpc"
    assert args.quick is True
    assert args.json is True
    assert args.func.__name__ == "cmd_verify"


def test_context_pack_parser_accepts_graph_notes_and_repo_filter() -> None:
    args = parse_args(
        [
            "context-pack",
            "MCP architecture",
            "--project",
            "gpc",
            "--repo",
            "core",
            "--include-graph",
            "--output",
            "pack.md",
        ]
    )

    assert args.query == "MCP architecture"
    assert args.project == "gpc"
    assert args.repo == ["core"]
    assert args.include_graph is True
    assert args.output == "pack.md"
    assert args.func.__name__ == "cmd_context_pack"


def test_maintenance_doctor_parser_is_dry_run_diagnostic_command() -> None:
    args = parse_args(["maintenance", "doctor", "--project", "gpc", "--json"])

    assert args.project == "gpc"
    assert args.json is True
    assert args.func.__name__ == "cmd_maintenance_doctor"


def test_mcp_usage_parser_accepts_since_window() -> None:
    args = parse_args(["mcp-usage", "--project", "gpc", "--since", "7d", "--json"])

    assert args.project == "gpc"
    assert args.since == "7d"
    assert args.json is True
    assert args.func.__name__ == "cmd_mcp_usage"


def test_metrics_collect_all_parser_supports_json_output() -> None:
    args = parse_args(["metrics", "collect-all", "--json"])

    assert args.json is True
    assert args.func.__name__ == "cmd_metrics_collect_all"


def test_metrics_collect_parser_accepts_scheduled_source() -> None:
    args = parse_args(["metrics", "collect", "--project", "gpc", "--source", "scheduled"])

    assert args.source == "scheduled"
    assert args.func.__name__ == "cmd_metrics_collect"


def test_audit_cross_project_parser_supports_output_and_json() -> None:
    args = parse_args(["audit", "cross-project", "--output", "docs/audit.md", "--json"])

    assert args.output == "docs/audit.md"
    assert args.json is True
    assert args.func.__name__ == "cmd_audit_cross_project"


def test_graph_test_portfolio_parser_accepts_test_roots_and_persist() -> None:
    args = parse_args(
        [
            "graph",
            "test-portfolio",
            "--project",
            "gpc",
            "--test-root",
            "tests",
            "--test-root",
            "tests/smoke",
            "--min-god-node-degree",
            "5",
            "--persist",
            "--json",
        ]
    )

    assert args.project == "gpc"
    assert args.test_roots == ["tests", "tests/smoke"]
    assert args.min_god_node_degree == 5
    assert args.persist is True
    assert args.json is True
    assert args.func.__name__ == "cmd_graph_test_portfolio"
