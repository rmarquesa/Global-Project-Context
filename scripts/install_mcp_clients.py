from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "gpc"
SUPPORTED_CLIENTS = (
    "codex",
    "claude-code",
    "claude-desktop",
    "openclaw",
    "gemini",
    "gemini-antigravity",
    "copilot",
)


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the GPC MCP server into local MCP-capable clients.",
    )
    parser.add_argument(
        "--clients",
        default="all",
        help=(
            "Comma-separated clients to configure: codex, claude-code, "
            "claude-desktop, openclaw, gemini, gemini-antigravity, "
            "copilot, or all."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without changing client configuration files.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Do not install; only validate existing client configuration.",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the local MCP smoke test during validation.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create timestamped backups before changing config files.",
    )
    args = parser.parse_args()

    clients = parse_clients(args.clients)
    config = server_config()
    validate_server_paths(config)

    if not args.validate_only:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for client in clients:
            install_client(
                client,
                server_config(client),
                timestamp=timestamp,
                dry_run=args.dry_run,
                backup=not args.no_backup,
            )

    if args.dry_run:
        print("dry_run=true; validation skipped")
        return 0

    ok = validate_installation(clients, skip_smoke=args.skip_smoke)
    return 0 if ok else 1


def parse_clients(value: str) -> list[str]:
    raw = [item.strip() for item in value.split(",") if item.strip()]
    if not raw or raw == ["all"]:
        return list(SUPPORTED_CLIENTS)

    unknown = sorted(set(raw) - set(SUPPORTED_CLIENTS))
    if unknown:
        raise SystemExit(f"Unsupported clients: {', '.join(unknown)}")

    return raw


def server_config(client: str | None = None) -> dict[str, object]:
    python = ROOT / "venv" / "bin" / "python"
    server = ROOT / "gpc_mcp_server.py"
    label = client or "unknown"
    return {
        "command": "/usr/bin/env",
        "args": [f"GPC_MCP_CLIENT={label}", str(python), str(server)],
        "cwd": str(ROOT),
    }


def validate_server_paths(config: dict[str, object]) -> None:
    command = Path(str(config["command"]))
    args = [
        Path(str(arg))
        for arg in config["args"]  # type: ignore[index]
        if str(arg).startswith("/")
    ]

    missing = [path for path in [command, *args] if not path.exists()]
    if missing:
        paths = ", ".join(str(path) for path in missing)
        raise SystemExit(f"Missing required GPC MCP paths: {paths}")


def install_client(
    client: str,
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    print(f"==> configuring {client}")

    if client == "codex":
        install_codex(config, timestamp=timestamp, dry_run=dry_run, backup=backup)
    elif client == "claude-code":
        install_claude_code(config, timestamp=timestamp, dry_run=dry_run, backup=backup)
    elif client == "claude-desktop":
        install_claude_desktop(
            config,
            timestamp=timestamp,
            dry_run=dry_run,
            backup=backup,
        )
    elif client == "openclaw":
        install_openclaw(config, timestamp=timestamp, dry_run=dry_run, backup=backup)
    elif client == "gemini":
        install_gemini(config, timestamp=timestamp, dry_run=dry_run, backup=backup)
    elif client == "gemini-antigravity":
        install_gemini_antigravity(
            config,
            timestamp=timestamp,
            dry_run=dry_run,
            backup=backup,
        )
    elif client == "copilot":
        install_copilot(config, timestamp=timestamp, dry_run=dry_run, backup=backup)
    else:
        raise AssertionError(f"unhandled client: {client}")


def install_codex(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    require_command("codex")
    backup_file(Path.home() / ".codex" / "config.toml", timestamp, dry_run, backup)
    run(["codex", "mcp", "remove", SERVER_NAME], dry_run=dry_run, check=False)
    run(
        [
            "codex",
            "mcp",
            "add",
            SERVER_NAME,
            "--",
            str(config["command"]),
            *[str(arg) for arg in config["args"]],  # type: ignore[index]
        ],
        dry_run=dry_run,
    )


def install_claude_code(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    require_command("claude")
    backup_file(Path.home() / ".claude.json", timestamp, dry_run, backup)
    backup_file(Path.home() / ".claude" / "settings.json", timestamp, dry_run, backup)
    run(["claude", "mcp", "remove", SERVER_NAME], dry_run=dry_run, check=False)
    run(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            SERVER_NAME,
            "--",
            str(config["command"]),
            *[str(arg) for arg in config["args"]],  # type: ignore[index]
        ],
        dry_run=dry_run,
    )


def install_claude_desktop(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    path = Path.home() / "Library" / "Application Support" / "Claude" / (
        "claude_desktop_config.json"
    )
    data = read_json_object(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"{path} has non-object mcpServers")

    if servers.get(SERVER_NAME) == config:
        print(f"{path}: already configured")
        return

    backup_file(path, timestamp, dry_run, backup)
    servers[SERVER_NAME] = config
    write_json(path, data, dry_run=dry_run)


def install_openclaw(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    require_command("openclaw")
    backup_file(Path.home() / ".openclaw" / "openclaw.json", timestamp, dry_run, backup)
    value = json.dumps(config, separators=(",", ":"))
    run(["openclaw", "mcp", "set", SERVER_NAME, value], dry_run=dry_run)


def install_gemini(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    path = Path.home() / ".gemini" / "settings.json"
    upsert_mcp_servers_config(
        path,
        config,
        container_key="mcpServers",
        timestamp=timestamp,
        dry_run=dry_run,
        backup=backup,
    )


def install_gemini_antigravity(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
    upsert_mcp_servers_config(
        path,
        config,
        container_key="mcpServers",
        timestamp=timestamp,
        dry_run=dry_run,
        backup=backup,
    )


def install_copilot(
    config: dict[str, object],
    *,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    path = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Code"
        / "User"
        / "mcp.json"
    )
    vscode_config = {
        "type": "stdio",
        "command": config["command"],
        "args": config["args"],
    }
    upsert_mcp_servers_config(
        path,
        vscode_config,
        container_key="servers",
        timestamp=timestamp,
        dry_run=dry_run,
        backup=backup,
    )


def upsert_mcp_servers_config(
    path: Path,
    config: dict[str, object],
    *,
    container_key: str,
    timestamp: str,
    dry_run: bool,
    backup: bool,
) -> None:
    data = read_json_object(path)
    servers = data.setdefault(container_key, {})
    if not isinstance(servers, dict):
        raise SystemExit(f"{path} has non-object {container_key}")

    if servers.get(SERVER_NAME) == config:
        print(f"{path}: already configured")
        return

    backup_file(path, timestamp, dry_run, backup)
    servers[SERVER_NAME] = config
    write_json(path, data, dry_run=dry_run)


def validate_installation(
    clients: Iterable[str],
    *,
    skip_smoke: bool,
) -> bool:
    ok = True

    if not skip_smoke:
        print("==> validating GPC MCP server")
        result = run(
            [
                str(ROOT / "venv" / "bin" / "python"),
                "-m",
                "tests.smoke.mcp_smoke_test",
            ],
            check=False,
        )
        ok = ok and result.returncode == 0 and "mcp_smoke_test=passed" in result.output

    for client in clients:
        config = server_config(client)
        print(f"==> validating {client}")
        if client == "codex":
            ok = validate_cli_output(["codex", "mcp", "get", SERVER_NAME]) and ok
        elif client == "claude-code":
            ok = validate_cli_output(["claude", "mcp", "get", SERVER_NAME]) and ok
        elif client == "claude-desktop":
            ok = validate_claude_desktop(config) and ok
        elif client == "openclaw":
            ok = (
                validate_cli_output(["openclaw", "mcp", "show", SERVER_NAME])
                and ok
            )
        elif client == "gemini":
            ok = (
                validate_json_mcp_config(
                    Path.home() / ".gemini" / "settings.json",
                    config,
                    container_key="mcpServers",
                )
                and ok
            )
        elif client == "gemini-antigravity":
            ok = (
                validate_json_mcp_config(
                    Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
                    config,
                    container_key="mcpServers",
                )
                and ok
            )
        elif client == "copilot":
            ok = (
                validate_json_mcp_config(
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Code"
                    / "User"
                    / "mcp.json",
                    {
                        "type": "stdio",
                        "command": config["command"],
                        "args": config["args"],
                    },
                    container_key="servers",
                )
                and ok
            )
        else:
            raise AssertionError(f"unhandled client: {client}")

    print("validation=passed" if ok else "validation=failed")
    return ok


def validate_cli_output(command: list[str]) -> bool:
    result = run(command, check=False)
    output = result.output.lower()
    failed_markers = (
        "no mcp server",
        "not found",
        "error:",
        "failed",
    )
    ok = result.returncode == 0 and not any(marker in output for marker in failed_markers)
    print("ok" if ok else "failed")
    return ok


def validate_claude_desktop(config: dict[str, object]) -> bool:
    path = Path.home() / "Library" / "Application Support" / "Claude" / (
        "claude_desktop_config.json"
    )
    try:
        data = read_json_object(path)
    except SystemExit as exc:
        print(exc)
        return False

    ok = data.get("mcpServers", {}).get(SERVER_NAME) == config
    print("ok" if ok else f"failed: {path} missing {SERVER_NAME}")
    return ok


def validate_json_mcp_config(
    path: Path,
    config: dict[str, object],
    *,
    container_key: str,
) -> bool:
    try:
        data = read_json_object(path)
    except SystemExit as exc:
        print(exc)
        return False

    ok = data.get(container_key, {}).get(SERVER_NAME) == config
    print("ok" if ok else f"failed: {path} missing {container_key}.{SERVER_NAME}")
    return ok


def read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")

    return data


def write_json(path: Path, data: dict[str, object], *, dry_run: bool) -> None:
    if dry_run:
        print(f"would write {path}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def backup_file(path: Path, timestamp: str, dry_run: bool, backup: bool) -> None:
    if not backup or not path.exists():
        return

    target = path.with_name(f"{path.name}.gpc-backup-{timestamp}")
    if dry_run:
        print(f"would backup {path} -> {target}")
        return

    shutil.copy2(path, target)
    print(f"backup {target}")


def require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"Required command not found on PATH: {command}")


def run(
    command: list[str],
    *,
    dry_run: bool = False,
    check: bool = True,
) -> RunResult:
    print(f"$ {shlex.join(command)}")
    if dry_run:
        return RunResult(returncode=0, output="")

    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    output = "\n".join(
        part.strip()
        for part in [completed.stdout, completed.stderr]
        if part and part.strip()
    )
    if output:
        print(output)

    if check and completed.returncode != 0:
        raise SystemExit(completed.returncode)

    return RunResult(returncode=completed.returncode, output=output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
