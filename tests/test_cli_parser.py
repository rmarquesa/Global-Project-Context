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
