from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.install_mcp_clients as installer

# ---------------------------------------------------------------------------
# parse_clients / server_config
# ---------------------------------------------------------------------------


def test_parse_clients_defaults_to_all_supported() -> None:
    assert installer.parse_clients("all") == list(installer.SUPPORTED_CLIENTS)


def test_parse_clients_empty_string_defaults_to_all_supported() -> None:
    assert installer.parse_clients("") == list(installer.SUPPORTED_CLIENTS)


def test_parse_clients_accepts_explicit_subset_and_trims_whitespace() -> None:
    assert installer.parse_clients(" codex, copilot ") == ["codex", "copilot"]


def test_parse_clients_rejects_unknown_client() -> None:
    with pytest.raises(SystemExit, match="Unsupported clients: bogus"):
        installer.parse_clients("codex,bogus")


def test_server_config_embeds_client_label_in_env_arg() -> None:
    config = installer.server_config("codex")

    assert config["command"] == "/usr/bin/env"
    assert config["args"][0] == "GPC_MCP_CLIENT=codex"
    assert config["cwd"] == str(installer.ROOT)


def test_server_config_defaults_label_to_unknown_when_no_client_given() -> None:
    config = installer.server_config()

    assert config["args"][0] == "GPC_MCP_CLIENT=unknown"


# ---------------------------------------------------------------------------
# validate_server_paths
# ---------------------------------------------------------------------------


def test_validate_server_paths_passes_when_all_paths_exist(tmp_path: Path) -> None:
    python = tmp_path / "python"
    python.touch()
    config = {"command": str(python), "args": [str(python)], "cwd": str(tmp_path)}

    installer.validate_server_paths(config)


def test_validate_server_paths_raises_on_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing-python"
    config = {"command": str(missing), "args": [], "cwd": str(tmp_path)}

    with pytest.raises(SystemExit, match="Missing required GPC MCP paths"):
        installer.validate_server_paths(config)


def test_validate_server_paths_ignores_non_path_args(tmp_path: Path) -> None:
    python = tmp_path / "python"
    python.touch()
    config = {
        "command": str(python),
        "args": ["GPC_MCP_CLIENT=codex", str(python)],
        "cwd": str(tmp_path),
    }

    installer.validate_server_paths(config)


# ---------------------------------------------------------------------------
# read_json_object / write_json
# ---------------------------------------------------------------------------


def test_read_json_object_returns_empty_dict_for_missing_file(tmp_path: Path) -> None:
    assert installer.read_json_object(tmp_path / "missing.json") == {}


def test_read_json_object_returns_empty_dict_for_blank_file(tmp_path: Path) -> None:
    path = tmp_path / "blank.json"
    path.write_text("   \n", encoding="utf-8")

    assert installer.read_json_object(path) == {}


def test_read_json_object_parses_valid_object(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    assert installer.read_json_object(path) == {"mcpServers": {}}


def test_read_json_object_raises_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SystemExit, match="is not valid JSON"):
        installer.read_json_object(path)


def test_read_json_object_raises_when_root_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SystemExit, match="must contain a JSON object"):
        installer.read_json_object(path)


def test_write_json_dry_run_does_not_touch_disk(tmp_path: Path, capsys) -> None:
    path = tmp_path / "nested" / "config.json"

    installer.write_json(path, {"a": 1}, dry_run=True)

    assert not path.exists()
    assert "would write" in capsys.readouterr().out


def test_write_json_creates_parent_dirs_and_writes_pretty_json(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"

    installer.write_json(path, {"a": 1}, dry_run=False)

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert path.read_text(encoding="utf-8").endswith("\n")


# ---------------------------------------------------------------------------
# backup_file
# ---------------------------------------------------------------------------


def test_backup_file_noop_when_source_missing(tmp_path: Path) -> None:
    installer.backup_file(
        tmp_path / "missing", "20260101-000000", dry_run=False, backup=True
    )

    assert list(tmp_path.iterdir()) == []


def test_backup_file_noop_when_backup_disabled(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")

    installer.backup_file(source, "20260101-000000", dry_run=False, backup=False)

    assert list(tmp_path.iterdir()) == [source]


def test_backup_file_dry_run_does_not_copy(tmp_path: Path, capsys) -> None:
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")

    installer.backup_file(source, "20260101-000000", dry_run=True, backup=True)

    assert list(tmp_path.iterdir()) == [source]
    assert "would backup" in capsys.readouterr().out


def test_backup_file_copies_with_timestamped_suffix(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text('{"k": "v"}', encoding="utf-8")

    installer.backup_file(source, "20260101-000000", dry_run=False, backup=True)

    backup = tmp_path / "config.json.gpc-backup-20260101-000000"
    assert backup.read_text(encoding="utf-8") == '{"k": "v"}'


# ---------------------------------------------------------------------------
# require_command / run
# ---------------------------------------------------------------------------


def test_require_command_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _cmd: None)

    with pytest.raises(SystemExit, match="Required command not found on PATH: codex"):
        installer.require_command("codex")


def test_require_command_passes_when_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _cmd: "/usr/bin/codex")

    installer.require_command("codex")


def test_run_dry_run_never_invokes_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(installer.subprocess, "run", fail_if_called)

    result = installer.run(["codex", "mcp", "add"], dry_run=True)

    assert result == installer.RunResult(returncode=0, output="")


def test_run_returns_combined_stdout_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompleted:
        returncode = 0
        stdout = "out-line"
        stderr = "err-line"

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: FakeCompleted())

    result = installer.run(["codex", "mcp", "get", "gpc"], check=False)

    assert result.returncode == 0
    assert "out-line" in result.output
    assert "err-line" in result.output


def test_run_raises_system_exit_on_nonzero_returncode_when_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompleted:
        returncode = 7
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: FakeCompleted())

    with pytest.raises(SystemExit) as excinfo:
        installer.run(["codex", "mcp", "add"], check=True)

    assert excinfo.value.code == 7


def test_run_does_not_raise_on_nonzero_returncode_when_check_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompleted:
        returncode = 7
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: FakeCompleted())

    result = installer.run(["codex", "mcp", "add"], check=False)

    assert result.returncode == 7


# ---------------------------------------------------------------------------
# upsert_mcp_servers_config
# ---------------------------------------------------------------------------


def test_upsert_mcp_servers_config_writes_new_entry(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    config = {"command": "/usr/bin/env", "args": ["gpc"], "cwd": "/repo"}

    installer.upsert_mcp_servers_config(
        path,
        config,
        container_key="mcpServers",
        timestamp="20260101-000000",
        dry_run=False,
        backup=True,
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"][installer.SERVER_NAME] == config


def test_upsert_mcp_servers_config_skips_when_already_identical(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "settings.json"
    config = {"command": "/usr/bin/env", "args": ["gpc"], "cwd": "/repo"}
    path.write_text(
        json.dumps({"mcpServers": {installer.SERVER_NAME: config}}),
        encoding="utf-8",
    )

    installer.upsert_mcp_servers_config(
        path,
        config,
        container_key="mcpServers",
        timestamp="20260101-000000",
        dry_run=False,
        backup=True,
    )

    assert "already configured" in capsys.readouterr().out
    # File must remain untouched (no backup created, no rewrite).
    assert not (tmp_path / "settings.json.gpc-backup-20260101-000000").exists()


def test_upsert_mcp_servers_config_raises_when_container_key_not_object(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"mcpServers": ["not", "an", "object"]}), encoding="utf-8"
    )

    with pytest.raises(SystemExit, match="has non-object mcpServers"):
        installer.upsert_mcp_servers_config(
            path,
            {"command": "x"},
            container_key="mcpServers",
            timestamp="20260101-000000",
            dry_run=False,
            backup=True,
        )


# ---------------------------------------------------------------------------
# install_claude_desktop / validate_claude_desktop (hardcoded Path.home())
# ---------------------------------------------------------------------------


def _claude_desktop_config_path(home: Path) -> Path:
    return (
        home
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def test_install_claude_desktop_writes_config_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    config = {"command": "/usr/bin/env", "args": ["gpc"], "cwd": "/repo"}

    installer.install_claude_desktop(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    data = json.loads(_claude_desktop_config_path(tmp_path).read_text(encoding="utf-8"))
    assert data["mcpServers"][installer.SERVER_NAME] == config


def test_validate_claude_desktop_true_when_config_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    config = {"command": "/usr/bin/env", "args": ["gpc"], "cwd": "/repo"}
    path = _claude_desktop_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {installer.SERVER_NAME: config}}), encoding="utf-8"
    )

    assert installer.validate_claude_desktop(config) is True


def test_validate_claude_desktop_false_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)

    assert installer.validate_claude_desktop({"command": "x"}) is False


# ---------------------------------------------------------------------------
# validate_json_mcp_config
# ---------------------------------------------------------------------------


def test_validate_json_mcp_config_true_when_matching(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    config = {"command": "/usr/bin/env", "args": ["gpc"], "cwd": "/repo"}
    path.write_text(
        json.dumps({"mcpServers": {installer.SERVER_NAME: config}}), encoding="utf-8"
    )

    assert (
        installer.validate_json_mcp_config(path, config, container_key="mcpServers")
        is True
    )


def test_validate_json_mcp_config_false_when_mismatched(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"mcpServers": {installer.SERVER_NAME: {"command": "other"}}}),
        encoding="utf-8",
    )

    assert (
        installer.validate_json_mcp_config(
            path, {"command": "/usr/bin/env"}, container_key="mcpServers"
        )
        is False
    )


def test_validate_json_mcp_config_false_on_invalid_json(tmp_path: Path, capsys) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")

    assert (
        installer.validate_json_mcp_config(
            path, {"command": "x"}, container_key="mcpServers"
        )
        is False
    )
    assert "not valid JSON" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# install_codex / install_claude_code / install_openclaw (CLI-based clients)
# ---------------------------------------------------------------------------


def test_install_codex_removes_then_adds_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(installer, "require_command", lambda _cmd: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        installer,
        "run",
        lambda command, **kwargs: calls.append(command) or installer.RunResult(0, ""),
    )

    config = installer.server_config("codex")
    installer.install_codex(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    assert calls[0] == ["codex", "mcp", "remove", installer.SERVER_NAME]
    assert calls[1][:4] == ["codex", "mcp", "add", installer.SERVER_NAME]


def test_install_claude_code_backs_up_two_files_and_adds_user_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(installer, "require_command", lambda _cmd: None)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        installer,
        "run",
        lambda command, **kwargs: calls.append(command) or installer.RunResult(0, ""),
    )

    config = installer.server_config("claude-code")
    installer.install_claude_code(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    assert (tmp_path / ".claude.json.gpc-backup-20260101-000000").exists()
    assert (tmp_path / ".claude" / "settings.json.gpc-backup-20260101-000000").exists()
    assert calls[1][:5] == ["claude", "mcp", "add", "--scope", "user"]


def test_install_openclaw_passes_json_config_as_single_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(installer, "require_command", lambda _cmd: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        installer,
        "run",
        lambda command, **kwargs: calls.append(command) or installer.RunResult(0, ""),
    )

    config = installer.server_config("openclaw")
    installer.install_openclaw(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    assert calls[0][:3] == ["openclaw", "mcp", "set"]
    assert json.loads(calls[0][4]) == config


# ---------------------------------------------------------------------------
# install_gemini / install_gemini_antigravity / install_copilot
# ---------------------------------------------------------------------------


def test_install_gemini_writes_under_dot_gemini_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    config = installer.server_config("gemini")

    installer.install_gemini(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    path = tmp_path / ".gemini" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"][installer.SERVER_NAME] == config


def test_install_gemini_antigravity_writes_under_antigravity_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    config = installer.server_config("gemini-antigravity")

    installer.install_gemini_antigravity(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    path = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"][installer.SERVER_NAME] == config


def test_install_copilot_writes_stdio_shaped_config_under_servers_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    config = installer.server_config("copilot")

    installer.install_copilot(
        config, timestamp="20260101-000000", dry_run=False, backup=True
    )

    path = tmp_path / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["servers"][installer.SERVER_NAME]
    assert entry == {
        "type": "stdio",
        "command": config["command"],
        "args": config["args"],
    }


# ---------------------------------------------------------------------------
# install_client dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client, expected_target",
    [
        ("codex", "install_codex"),
        ("claude-code", "install_claude_code"),
        ("claude-desktop", "install_claude_desktop"),
        ("openclaw", "install_openclaw"),
        ("gemini", "install_gemini"),
        ("gemini-antigravity", "install_gemini_antigravity"),
        ("copilot", "install_copilot"),
    ],
)
def test_install_client_dispatches_to_matching_installer(
    client: str, expected_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in (
        "install_codex",
        "install_claude_code",
        "install_claude_desktop",
        "install_openclaw",
        "install_gemini",
        "install_gemini_antigravity",
        "install_copilot",
    ):
        monkeypatch.setattr(
            installer, name, (lambda tag: lambda *a, **k: calls.append(tag))(name)
        )

    installer.install_client(
        client, {"command": "x"}, timestamp="ts", dry_run=False, backup=True
    )

    assert calls == [expected_target]


def test_install_client_raises_for_unhandled_client() -> None:
    with pytest.raises(AssertionError, match="unhandled client: bogus"):
        installer.install_client(
            "bogus", {"command": "x"}, timestamp="ts", dry_run=False, backup=True
        )


# ---------------------------------------------------------------------------
# validate_cli_output / validate_installation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, returncode, expected",
    [
        ("server: gpc\ncommand: ...", 0, True),
        ("Error: no mcp server found", 0, False),
        ("ok", 1, False),
    ],
)
def test_validate_cli_output(
    output: str,
    returncode: int,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        installer,
        "run",
        lambda *a, **k: installer.RunResult(returncode=returncode, output=output),
    )

    assert installer.validate_cli_output(["codex", "mcp", "get", "gpc"]) is expected


def test_validate_installation_runs_smoke_test_and_dispatches_per_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        run_calls.append(command)
        return installer.RunResult(returncode=0, output="mcp_smoke_test=passed")

    monkeypatch.setattr(installer, "run", fake_run)
    monkeypatch.setattr(installer, "validate_cli_output", lambda _cmd: True)
    monkeypatch.setattr(installer, "validate_claude_desktop", lambda _cfg: True)
    monkeypatch.setattr(installer, "validate_json_mcp_config", lambda *_a, **_k: True)

    ok = installer.validate_installation(
        list(installer.SUPPORTED_CLIENTS), skip_smoke=False
    )

    assert ok is True
    assert run_calls  # smoke test invoked
    assert any("tests.smoke.mcp_smoke_test" in str(part) for part in run_calls[0])


def test_validate_installation_skips_smoke_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        installer,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run() should not be called")
        ),
    )
    monkeypatch.setattr(installer, "validate_cli_output", lambda _cmd: True)

    ok = installer.validate_installation(["codex"], skip_smoke=True)

    assert ok is True


def test_validate_installation_false_when_any_client_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        installer,
        "run",
        lambda *a, **k: installer.RunResult(
            returncode=0, output="mcp_smoke_test=passed"
        ),
    )
    monkeypatch.setattr(installer, "validate_cli_output", lambda _cmd: False)

    ok = installer.validate_installation(["codex"], skip_smoke=False)

    assert ok is False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_dry_run_skips_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.sys, "argv", ["install_mcp_clients.py", "--dry-run"])
    monkeypatch.setattr(installer, "validate_server_paths", lambda _cfg: None)
    install_calls: list[str] = []
    monkeypatch.setattr(
        installer,
        "install_client",
        lambda client, *a, **k: install_calls.append(client),
    )
    monkeypatch.setattr(
        installer,
        "validate_installation",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("validate_installation should not run in dry-run mode")
        ),
    )

    assert installer.main() == 0
    assert install_calls == list(installer.SUPPORTED_CLIENTS)


def test_main_validate_only_skips_install_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        installer.sys,
        "argv",
        ["install_mcp_clients.py", "--validate-only", "--clients", "codex"],
    )
    monkeypatch.setattr(installer, "validate_server_paths", lambda _cfg: None)
    monkeypatch.setattr(
        installer,
        "install_client",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("install_client should not run with --validate-only")
        ),
    )
    monkeypatch.setattr(installer, "validate_installation", lambda *a, **k: True)

    assert installer.main() == 0


def test_main_returns_1_when_validation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        installer.sys, "argv", ["install_mcp_clients.py", "--clients", "codex"]
    )
    monkeypatch.setattr(installer, "validate_server_paths", lambda _cfg: None)
    monkeypatch.setattr(installer, "install_client", lambda *a, **k: None)
    monkeypatch.setattr(installer, "validate_installation", lambda *a, **k: False)

    assert installer.main() == 1
