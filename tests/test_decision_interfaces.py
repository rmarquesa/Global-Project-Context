"""Decision CLI/MCP contracts without external services or persistent writes."""

from __future__ import annotations

import asyncio
import json
import shlex
from unittest.mock import MagicMock

import psycopg
from psycopg.errors import UndefinedColumn, UndefinedTable
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from gpc import cli, decisions, mcp_observability, mcp_server
from tests.test_decisions import IsolatedDatabase

PROJECT = {"id": "p-id", "slug": "p", "name": "P", "root_path": "/p"}
ID = "00000000-0000-0000-0000-000000000001"
SCOPE = {"project": "product", "repo": "api"}
WRITE = "--project product --repo api --actor maintainer"
CAPTURE = (
    "decision capture " + WRITE + " --key auth-policy --title 'Use local policy' "
    "--content 'Explicit local policy' --source-path src/auth.py"
)
ROUTES = [
    (
        CAPTURE + " --source-revision release-label",
        "capture_decision",
        (),
        dict(
            **SCOPE,
            actor="maintainer",
            decision_key="auth-policy",
            title="Use local policy",
            content="Explicit local policy",
            source_path="src/auth.py",
            source_revision="release-label",
        ),
    ),
    (
        f"decision approve {ID} {WRITE} --reason reviewed --supersede {ID}",
        "approve_decision",
        (ID,),
        dict(**SCOPE, actor="maintainer", reason="reviewed", supersede=ID),
    ),
    (
        f"decision invalidate {ID} {WRITE} --reason obsolete",
        "invalidate_decision",
        (ID,),
        dict(**SCOPE, actor="maintainer", reason="obsolete"),
    ),
    (
        f"decision show {ID} --project product --repo api",
        "get_decision",
        (ID,),
        SCOPE,
    ),
    (
        "decision list --project product --repo api --path src/auth.py "
        "--query policy --limit 3 --include-inactive",
        "list_decisions",
        (),
        dict(
            **SCOPE, path="src/auth.py", query="policy", limit=3, include_inactive=True
        ),
    ),
]


def row(**overrides):
    return (
        dict(
            id=ID,
            repo_slug="api",
            decision_key="auth-policy",
            title="Use local policy",
            content="Explicit local policy",
            source_path="src/auth.py",
            source_hash="hash",
            source_revision="release-label",
            status="pending",
            freshness="current",
            events=[{"action": "capture", "actor": "maintainer"}],
        )
        | overrides
    )


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("External service access is forbidden in decision interface tests")

    for module in (decisions, mcp_server, mcp_observability):
        monkeypatch.setattr(module, "pg_connection", forbidden)
    log = MagicMock(return_value=None)
    samples = MagicMock()
    monkeypatch.setattr(mcp_observability, "_write_log", log)
    monkeypatch.setattr(mcp_observability, "_write_token_savings_sample", samples)
    return log, samples


@pytest.mark.parametrize("command,symbol,positional,keywords", ROUTES)
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_actual_routes_forward_once_and_display(
    monkeypatch, capsys, command, symbol, positional, keywords, as_json
):
    returned = [row()] if symbol == "list_decisions" else row()
    backend = MagicMock(return_value=returned)
    monkeypatch.setattr(decisions, symbol, backend)
    assert cli.main(shlex.split(command) + (["--json"] if as_json else [])) == 0
    backend.assert_called_once_with(*positional, **keywords)
    captured = capsys.readouterr()
    assert not captured.err
    if as_json:
        assert json.loads(captured.out) == returned
    else:
        for evidence in (ID, "status=pending", "freshness=current", "source_hash=hash"):
            assert evidence in captured.out
        assert "caller-provided, unverified" in captured.out
        assert "indexed content hash, not live files" in captured.out
        if symbol == "capture_decision":
            assert "explicit approval is required" in captured.out
        if symbol == "get_decision":
            assert (
                "history=" in captured.out and '"actor": "maintainer"' in captured.out
            )
            assert "not an endorsement" in captured.out


@pytest.mark.parametrize("command,symbol,positional,keywords", ROUTES)
def test_documented_examples_parse(command, symbol, positional, keywords):
    args = cli.build_parser().parse_args(shlex.split(command))
    assert args.func.__name__ == "cmd_decision"
    assert args.project == "product"
    assert args.json is False


@pytest.mark.parametrize(
    "argv",
    [
        ["decision"],
        ["decision", "unknown"],
        ["decision", "list"],
        ["decision", "show", ID],
        shlex.split(CAPTURE.replace("--key auth-policy", "")),
        shlex.split(CAPTURE.replace("--title 'Use local policy'", "")),
        shlex.split(CAPTURE.replace("--content 'Explicit local policy'", "")),
        shlex.split(CAPTURE.replace("--source-path src/auth.py", "")),
        shlex.split(f"decision invalidate {ID} {WRITE}"),
    ],
)
def test_parser_rejects_incomplete_or_unknown_without_backend(monkeypatch, argv):
    writers = [MagicMock() for _ in range(3)]
    for name, writer in zip(
        ("capture_decision", "approve_decision", "invalidate_decision"), writers
    ):
        monkeypatch.setattr(decisions, name, writer)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    for writer in writers:
        writer.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        CAPTURE,
        f"decision approve {ID} {WRITE}",
        f"decision invalidate {ID} {WRITE} --reason obsolete",
    ],
)
@pytest.mark.parametrize(
    "flag,value",
    [("--project", "product"), ("--repo", "api"), ("--actor", "maintainer")],
)
def test_every_write_requires_explicit_scope_and_actor(command, flag, value):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(
            shlex.split(command.replace(f"{flag} {value}", ""))
        )
    assert exc.value.code == 2


@pytest.mark.parametrize("action", ["capture", "approve", "invalidate", "show", "list"])
def test_help_does_not_write(monkeypatch, action):
    writers = []
    for symbol in ("capture_decision", "approve_decision", "invalidate_decision"):
        writer = MagicMock()
        writers.append(writer)
        monkeypatch.setattr(decisions, symbol, writer)
    with pytest.raises(SystemExit) as exc:
        cli.main(["decision", action, "--help"])
    assert exc.value.code == 0
    for writer in writers:
        writer.assert_not_called()


def test_parser_read_defaults_and_optional_write_fields():
    args = cli.build_parser().parse_args(["decision", "list", "--project", "p"])
    assert (args.repo, args.path, args.query, args.limit, args.include_inactive) == (
        None,
        None,
        None,
        20,
        False,
    )
    args = cli.build_parser().parse_args(shlex.split(CAPTURE))
    assert args.source_revision is None
    args = cli.build_parser().parse_args(shlex.split(f"decision approve {ID} {WRITE}"))
    assert args.reason is args.supersede is None


@pytest.mark.parametrize(
    "exception,expected",
    [
        (
            decisions.DecisionError("Source must have current indexed hash evidence"),
            "indexed hash",
        ),
        (
            decisions.DecisionConflictError("Decision key is already approved"),
            "already approved",
        ),
        (UndefinedTable("postgresql://user:SECRET@db/table"), "migration 0011"),
        (UndefinedColumn("postgresql://user:SECRET@db/column"), "migration 0011"),
        (
            psycopg.OperationalError("postgresql://user:SECRET@db"),
            "database operation failed",
        ),
    ],
)
def test_cli_known_errors_nonzero_safe(monkeypatch, capsys, exception, expected):
    monkeypatch.setattr(decisions, "list_decisions", MagicMock(side_effect=exception))
    assert cli.main(["decision", "list", "--project", "p", "--json"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert expected in captured.err
    assert "SECRET" not in captured.err and "Traceback" not in captured.err


def test_cli_does_not_globally_swallow_programming_errors(monkeypatch):
    monkeypatch.setattr(
        decisions, "list_decisions", MagicMock(side_effect=RuntimeError("bug"))
    )
    with pytest.raises(RuntimeError, match="bug"):
        cli.main(["decision", "list", "--project", "p"])


def test_actual_tool_registry_has_exact_read_only_schema():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = [tool.name for tool in tools if "decision" in tool.name]
    assert names == ["gpc_decisions"]
    tool = next(tool for tool in tools if tool.name == "gpc_decisions")
    props = tool.inputSchema["properties"]
    assert set(props) == {
        "query",
        "project",
        "cwd",
        "repo",
        "path",
        "limit",
        "content_chars",
        "include_inactive",
    }
    assert not tool.inputSchema.get("required")
    assert {name: props[name]["default"] for name in props} == dict(
        query="",
        project=None,
        cwd=None,
        repo=None,
        path=None,
        limit=5,
        content_chars=1200,
        include_inactive=False,
    )
    assert props["limit"]["type"] == props["content_chars"]["type"] == "integer"
    assert props["include_inactive"]["type"] == "boolean"
    assert props["repo"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "never captures" in tool.description


@pytest.mark.parametrize(
    "project,cwd,effective",
    [
        ("alias", "/caller", "/caller"),
        (None, "/workspace/subdir", "/workspace/subdir"),
        (None, None, "/configured-workspace"),
    ],
)
@pytest.mark.parametrize("repo", [None, "api", "web"])
def test_mcp_resolves_alias_or_cwd_and_preserves_multirepo_filter(
    monkeypatch, no_external_services, project, cwd, effective, repo
):
    monkeypatch.setenv("GPC_CALLER_CWD", "/configured-workspace")
    resolver = MagicMock(return_value=PROJECT)
    retrieve = MagicMock(return_value=[row(status="approved")])
    monkeypatch.setattr(mcp_server, "resolve_project", resolver)
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    payload = mcp_server.mcp_decisions(
        "policy",
        project=project,
        cwd=cwd,
        repo=repo,
        path="src/auth.py",
        limit=2,
        content_chars=4,
        include_inactive=True,
    )
    resolver.assert_called_once_with(project=project, cwd=effective)
    retrieve.assert_called_once_with(
        project="p",
        repo=repo,
        path="src/auth.py",
        query="policy",
        limit=2,
        include_inactive=True,
    )
    assert payload["ok"] and payload["project"]["slug"] == "p"
    assert payload["query"] == "policy" and payload["repo_filter"] == repo
    assert payload["path"] == "src/auth.py" and payload["include_inactive"] is True
    assert payload["evidence_basis"] == "indexed_content_hash"
    item = payload["decisions"][0]
    assert item["content"] == "Expl" and item["content_truncated"] is True
    assert item["status"] == "approved" and item["freshness"] == "current"
    assert item["source_hash"] == "hash" and item["source_path"] == "src/auth.py"
    assert "Caller-provided" in item["source_revision_note"]
    assert "not a verified commit" in item["source_revision_note"]
    assert "events" not in item
    assert json.loads(json.dumps(payload)) == payload
    log, samples = no_external_services
    assert log.call_args.kwargs["tool"] == "gpc_decisions"
    assert log.call_args.kwargs["success"] is True
    samples.assert_called_once()


@pytest.mark.parametrize(
    "invalid",
    [
        {"limit": True},
        {"limit": "2"},
        {"limit": 2.1},
        {"limit": None},
        {"content_chars": False},
        {"content_chars": "20"},
        {"content_chars": 1.5},
        {"include_inactive": 1},
        {"include_inactive": "false"},
        {"query": None},
        {"query": 1},
        {"query": "x" * 513},
        {"query": " " * 513},
        {"query": "a\x00b"},
    ],
)
def test_mcp_invalid_types_fail_before_resolution_or_backend(monkeypatch, invalid):
    resolve = MagicMock()
    retrieve = MagicMock()
    monkeypatch.setattr(mcp_server, "resolve_project", resolve)
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    payload = mcp_server.mcp_decisions(**invalid)
    assert payload["ok"] is False and payload["error"]["type"] == "DecisionError"
    resolve.assert_not_called()
    retrieve.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        {"limit": True},
        {"limit": "2"},
        {"limit": 2.1},
        {"content_chars": False},
        {"content_chars": "20"},
        {"include_inactive": 1},
        {"include_inactive": "false"},
    ],
)
def test_registered_mcp_dispatch_does_not_coerce_invalid_types(monkeypatch, invalid):
    resolver = MagicMock()
    retrieve = MagicMock()
    monkeypatch.setattr(mcp_server, "resolve_project", resolver)
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.mcp.call_tool("gpc_decisions", invalid))
    resolver.assert_not_called()
    retrieve.assert_not_called()


def test_registered_mcp_dispatch_reaches_only_read_backend(monkeypatch):
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    retrieve = MagicMock(return_value=[row(status="approved")])
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    _, payload = asyncio.run(
        mcp_server.mcp.call_tool(
            "gpc_decisions", {"query": "policy", "repo": "api", "content_chars": 0}
        )
    )
    retrieve.assert_called_once_with(
        project="p",
        repo="api",
        path=None,
        query="policy",
        limit=5,
        include_inactive=False,
    )
    assert payload["ok"] and payload["decisions"][0]["content"] == ""
    assert payload["decisions"][0]["content_truncated"] is True


@pytest.mark.parametrize("query", ["", "   ", " " * 512])
def test_mcp_blank_query_is_none_after_size_check(monkeypatch, query):
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    retrieve = MagicMock(return_value=[])
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    assert mcp_server.mcp_decisions(query)["ok"]
    retrieve.assert_called_once_with(
        project="p", repo=None, path=None, query=None, limit=5, include_inactive=False
    )


@pytest.mark.parametrize(
    "limit,chars,expected_limit,expected_chars",
    [
        (0, -1, 1, 0),
        (500, 50000, 20, 4000),
        (2, 20, 2, 20),
    ],
)
def test_mcp_clamps_bounds_even_for_corrupted_or_oversized_backend_rows(
    monkeypatch, limit, chars, expected_limit, expected_chars
):
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    oversized = row(
        content="x" * 10000,
        title="t" * 999,
        source_path="p" * 999,
        source_revision="r" * 999,
        source_hash="h" * 999,
        approved_by="a" * 999,
        metadata={"secret": "must not leak"},
        events=[{"secret": "must not leak"}],
        unexpected="must not leak",
    )
    retrieve = MagicMock(return_value=[oversized] * 30)
    monkeypatch.setattr(mcp_server, "list_decisions", retrieve)
    payload = mcp_server.mcp_decisions(
        limit=limit, content_chars=chars, include_inactive=True
    )
    assert retrieve.call_args.kwargs["limit"] == expected_limit
    assert len(payload["decisions"]) == expected_limit
    item = payload["decisions"][0]
    assert len(item["content"]) == expected_chars and item["content_truncated"] is True
    for name, bound in (
        ("title", 240),
        ("source_path", 512),
        ("source_revision", 128),
        ("source_hash", 128),
        ("approved_by", 128),
    ):
        assert len(item[name]) == bound
    assert item["diagnostic_only"] is True
    assert "must not leak" not in json.dumps(payload)
    assert "metadata" not in item and "events" not in item and "unexpected" not in item


@pytest.mark.parametrize(
    "status,freshness,diagnostic",
    [
        ("pending", "current", True),
        ("invalidated", "current", True),
        ("superseded", "current", True),
        ("legacy", "unverified", True),
        ("approved", "stale", True),
        ("approved", "missing_source", True),
        ("approved", "current", False),
    ],
)
def test_mcp_status_and_freshness_cannot_be_mistaken_for_endorsement(
    monkeypatch, status, freshness, diagnostic
):
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    monkeypatch.setattr(
        mcp_server,
        "list_decisions",
        MagicMock(return_value=[row(status=status, freshness=freshness)]),
    )
    item = mcp_server.mcp_decisions(include_inactive=True)["decisions"][0]
    assert (item["status"], item["freshness"], item["diagnostic_only"]) == (
        status,
        freshness,
        diagnostic,
    )


@pytest.mark.parametrize(
    "exception,error_type,hint",
    [
        (
            decisions.DecisionError(
                "source_path must be a relative POSIX path without traversal"
            ),
            "DecisionError",
            "relative POSIX",
        ),
        (
            UndefinedTable("SECRET_DSN table sql"),
            "DecisionSchemaUnavailable",
            "migration 0011",
        ),
        (
            UndefinedColumn("SECRET_DSN column sql"),
            "DecisionSchemaUnavailable",
            "migration 0011",
        ),
        (
            psycopg.OperationalError("SECRET_DSN"),
            "DecisionRetrievalError",
            "retrieval failed",
        ),
        (
            RuntimeError("SECRET_DSN private content"),
            "DecisionRetrievalError",
            "retrieval failed",
        ),
    ],
)
@pytest.mark.parametrize("failing_stage", ["resolve_project", "list_decisions"])
def test_mcp_backend_errors_are_safe_bounded_and_logged(
    monkeypatch, no_external_services, exception, error_type, hint, failing_stage
):
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    monkeypatch.setattr(mcp_server, "list_decisions", MagicMock(return_value=[]))
    monkeypatch.setattr(mcp_server, failing_stage, MagicMock(side_effect=exception))
    payload = mcp_server.mcp_decisions()
    assert payload["ok"] is False and payload["error"]["type"] == error_type
    assert hint in payload["error"]["message"]
    assert "SECRET_DSN" not in json.dumps(payload) and len(json.dumps(payload)) < 700
    assert no_external_services[0].call_args.kwargs["success"] is False


@pytest.fixture
def isolated_backend(monkeypatch):
    db = IsolatedDatabase()
    monkeypatch.setattr(decisions, "pg_connection", db.connect)
    monkeypatch.setattr(decisions, "resolve_repo", db.resolve)
    monkeypatch.setattr(mcp_server, "resolve_project", MagicMock(return_value=PROJECT))
    yield db
    db.conn.close()


def test_cli_to_backend_to_mcp_integration_is_scoped_current_and_read_only(
    monkeypatch, capsys, isolated_backend
):
    db = isolated_backend

    def capture(key, repo="a"):
        assert (
            cli.main(
                [
                    "decision",
                    "capture",
                    "--project",
                    "p",
                    "--repo",
                    repo,
                    "--actor",
                    "human",
                    "--key",
                    key,
                    "--title",
                    key,
                    "--content",
                    "Explicitly selected local policy.",
                    "--source-path",
                    "ADR.md",
                    "--json",
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    pending = capture("pending")
    current = capture("current")
    other_repo = capture("other-repo", "b")
    for decision, repo in ((current, "a"), (other_repo, "b")):
        assert (
            cli.main(
                [
                    "decision",
                    "approve",
                    decision["id"],
                    "--project",
                    "p",
                    "--repo",
                    repo,
                    "--actor",
                    "reviewer",
                    "--json",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "approved"

    # The MCP tool must never call lifecycle mutators, even for inactive reads.
    for symbol in ("capture_decision", "approve_decision", "invalidate_decision"):
        monkeypatch.setattr(
            decisions, symbol, MagicMock(side_effect=AssertionError("read only"))
        )
    before = db.snapshot()
    start = len(db.statements)
    default = mcp_server.mcp_decisions(project="alias")
    assert {item["id"] for item in default["decisions"]} == {
        current["id"],
        other_repo["id"],
    }
    scoped = mcp_server.mcp_decisions(repo="a")
    assert [item["id"] for item in scoped["decisions"]] == [current["id"]]
    diagnostic = mcp_server.mcp_decisions(repo="a", include_inactive=True)
    assert {item["id"] for item in diagnostic["decisions"]} == {
        pending["id"],
        current["id"],
    }
    assert next(
        item for item in diagnostic["decisions"] if item["id"] == pending["id"]
    )["diagnostic_only"]
    assert db.snapshot() == before
    assert all(
        sql.lstrip().lower().startswith("select") for sql, _ in db.statements[start:]
    )

    db.conn.execute(
        "update gpc_files set content_hash = 'changed' where id = ?",
        (current["source_file_id"],),
    )
    db.conn.commit()
    assert mcp_server.mcp_decisions(repo="a")["decisions"] == []
    stale = mcp_server.mcp_decisions(repo="a", include_inactive=True)["decisions"]
    assert all(
        item["freshness"] == "stale" and item["diagnostic_only"] for item in stale
    )


@pytest.mark.parametrize(
    "filters", [{"path": "../secret"}, {"query": "a" * 513}, {"repo": "missing"}]
)
def test_mcp_backend_validation_is_not_bypassed(isolated_backend, filters):
    payload = mcp_server.mcp_decisions(**filters)
    assert payload["ok"] is False and payload["error"]["type"] == "DecisionError"
