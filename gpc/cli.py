from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

import psycopg
from qdrant_client import QdrantClient

from gpc.config import (
    COLLECTION_NAME,
    MCP_HTTP_HOST,
    MCP_HTTP_PATH,
    MCP_HTTP_PORT,
    POSTGRES_DSN,
    QDRANT_HOST,
    QDRANT_PORT,
    ROOT_DIR,
)
from gpc.embeddings import active_embedding_model, embedding_dimension
from gpc.indexer import IndexOptions, index_project_path
from gpc.registry import add_project_alias, normalize_slug
from gpc.search import search_project_context
from gpc.status import get_index_status
from gpc.token_economy import estimate_for_project


HOOKS = ("post-commit", "post-merge", "post-checkout")
MANAGED_HOOK_MARKER = "GPC managed hook"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpc",
        description="Global Project Context command line interface.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Index a project into GPC.")
    add_index_args(index_parser)
    index_parser.set_defaults(func=cmd_index)

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a project with .gpc.yaml, optional hooks, and initial index.",
    )
    init_parser.add_argument("path", nargs="?", default=".", help="Project root path.")
    init_parser.add_argument("--slug", help="Project slug. Defaults to directory name.")
    init_parser.add_argument("--name", help="Project display name.")
    init_parser.add_argument("--description", help="Optional project description.")
    init_parser.add_argument("--alias", action="append", default=[], help="Project alias.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite managed files.")
    init_parser.add_argument("--no-hooks", action="store_true", help="Do not install Git hooks.")
    init_parser.add_argument("--foreground-hooks", action="store_true", help="Run hook indexing in foreground.")
    init_parser.add_argument("--no-index", action="store_true", help="Skip initial indexing.")
    init_parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Do not add .gpc/ to the project .gitignore.",
    )
    init_parser.set_defaults(func=cmd_init)

    status_parser = subparsers.add_parser("status", help="Show index status.")
    status_parser.add_argument("--project", help="Project slug or alias.")
    status_parser.add_argument("--cwd", default=str(Path.cwd()), help="Project resolution cwd.")
    status_parser.add_argument("--runs", type=int, default=3, help="Recent runs to print.")
    status_parser.set_defaults(func=cmd_status)

    search_parser = subparsers.add_parser("search", help="Search indexed context.")
    search_parser.add_argument("query", help="Semantic search query.")
    search_parser.add_argument("--project", help="Project slug or alias.")
    search_parser.add_argument("--cwd", default=str(Path.cwd()), help="Project resolution cwd.")
    search_parser.add_argument("--limit", type=int, default=5, help="Number of results.")
    search_parser.add_argument("--content-chars", type=int, default=700, help="Characters per result.")
    search_parser.set_defaults(func=cmd_search)

    token_parser = subparsers.add_parser("token-savings", help="Estimate token savings.")
    token_parser.add_argument("query", help="Question or task to retrieve context for.")
    token_parser.add_argument("--project", action="append", help="Project slug or alias. Repeatable.")
    token_parser.add_argument("--cwd", help="Project resolution cwd.")
    token_parser.add_argument("--max-chunks", type=int, default=5)
    token_parser.add_argument("--max-chars", type=int, default=6_000)
    token_parser.add_argument("--json", action="store_true")
    token_parser.set_defaults(func=cmd_token_savings)

    doctor_parser = subparsers.add_parser("doctor", help="Check local GPC dependencies.")
    doctor_parser.set_defaults(func=cmd_doctor)

    shims_parser = subparsers.add_parser("install-shims", help="Install global shell commands.")
    shims_parser.add_argument(
        "--bin-dir",
        default=str(Path.home() / ".local" / "bin"),
        help="Directory for command shims. Default: ~/.local/bin.",
    )
    shims_parser.set_defaults(func=cmd_install_shims)

    clients_parser = subparsers.add_parser("install-clients", help="Install MCP client config.")
    clients_parser.add_argument("--clients", default="all")
    clients_parser.add_argument("--dry-run", action="store_true")
    clients_parser.add_argument("--validate-only", action="store_true")
    clients_parser.add_argument("--skip-smoke", action="store_true")
    clients_parser.add_argument("--no-backup", action="store_true")
    clients_parser.set_defaults(func=cmd_install_clients)

    migrate_parser = subparsers.add_parser("migrate", help="Run Postgres migrations.")
    migrate_parser.add_argument("migration_command", nargs="?", default="up", choices=("up", "status"))
    migrate_parser.set_defaults(func=cmd_migrate)

    qdrant_parser = subparsers.add_parser("init-qdrant", help="Initialize Qdrant collection.")
    qdrant_parser.add_argument("--reset", action="store_true")
    qdrant_parser.set_defaults(func=cmd_init_qdrant)

    http_parser = subparsers.add_parser("mcp-http", help="Run the MCP server over streamable HTTP.")
    http_parser.add_argument("--host", default=MCP_HTTP_HOST)
    http_parser.add_argument("--port", type=int, default=MCP_HTTP_PORT)
    http_parser.add_argument("--path", default=MCP_HTTP_PATH)
    http_parser.set_defaults(func=cmd_mcp_http)

    mcp_parser = subparsers.add_parser("mcp-stdio", help="Run the MCP server over stdio.")
    mcp_parser.set_defaults(func=cmd_mcp_stdio)

    return parser


def add_index_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", nargs="?", default=".", help="Project root path.")
    parser.add_argument("--slug", help="Project slug. Defaults to directory name.")
    parser.add_argument("--name", help="Project display name.")
    parser.add_argument("--description", help="Optional project description.")
    parser.add_argument("--reset", action="store_true", help="Delete existing index first.")
    parser.add_argument("--force", action="store_true", help="Re-embed unchanged files.")
    parser.add_argument("--no-prune", action="store_true", help="Do not prune missing files.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first file error.")
    parser.add_argument("--include-unknown-text", action="store_true")
    parser.add_argument("--follow-symlinks", action="store_true")
    parser.add_argument("--limit-files", type=int)
    parser.add_argument("--max-file-bytes", type=int, default=512_000)
    parser.add_argument("--chunk-chars", type=int, default=3_500)
    parser.add_argument("--batch-size", type=int, default=16)


def cmd_index(args: argparse.Namespace) -> int:
    stats = index_project_path(
        Path(args.path),
        slug=args.slug,
        name=args.name,
        description=args.description,
        options=IndexOptions(
            reset=args.reset,
            force=args.force,
            prune_deleted=not args.no_prune,
            fail_fast=args.fail_fast,
            include_unknown_text=args.include_unknown_text,
            follow_symlinks=args.follow_symlinks,
            max_file_bytes=args.max_file_bytes,
            chunk_chars=args.chunk_chars,
            batch_size=args.batch_size,
            limit_files=args.limit_files,
        ),
    )
    print_index_stats(stats)
    return 1 if stats.files_failed else 0


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Project root does not exist or is not a directory: {root}")

    slug = normalize_slug(args.slug or root.name)
    name = args.name or root.name
    write_project_config(
        root,
        slug=slug,
        name=name,
        description=args.description,
        aliases=args.alias,
        force=args.force,
    )

    if not args.no_gitignore:
        ensure_gitignore_entry(root)

    if not args.no_hooks:
        install_git_hooks(root, force=args.force, background=not args.foreground_hooks)

    if args.no_index:
        return 0

    stats = index_project_path(
        root,
        slug=slug,
        name=name,
        description=args.description,
        options=IndexOptions(),
    )
    for alias in args.alias:
        add_project_alias(slug, alias)
    print_index_stats(stats)
    return 1 if stats.files_failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    status = get_index_status(project=args.project, cwd=args.cwd, runs=args.runs)
    project = status["project"]
    print(
        f"project={project['slug']} files={status['files']} chunks={status['chunks']} "
        f"qdrant_points={status['qdrant_points']}"
    )
    for run in status["runs"]:
        metadata = run["metadata"] or {}
        print(
            "run "
            f"status={run['status']} files_seen={run['files_seen']} "
            f"files_indexed={run['files_indexed']} "
            f"files_unchanged={metadata.get('files_unchanged', 0)} "
            f"files_failed={metadata.get('files_failed', 0)} "
            f"chunks_written={run['chunks_written']} "
            f"started_at={run['started_at']}"
        )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    project, results = search_project_context(
        args.query,
        project=args.project,
        cwd=args.cwd,
        limit=args.limit,
    )
    print(f"project={project['slug']} results={len(results)}")
    for index, result in enumerate(results, start=1):
        content = result.content.strip().replace("\n\n\n", "\n\n")
        if len(content) > args.content_chars:
            content = f"{content[:args.content_chars].rstrip()}..."
        print(
            f"\n[{index}] score={result.score:.4f} "
            f"path={result.relative_path} chunk={result.chunk_id}"
        )
        print(content)
    return 0


def cmd_token_savings(args: argparse.Namespace) -> int:
    rows = [
        estimate_for_project(
            args.query,
            project=project,
            cwd=args.cwd,
            max_chunks=args.max_chunks,
            max_chars=args.max_chars,
        )
        for project in (args.project or [None])
    ]
    if args.json:
        print(json.dumps({"query": args.query, "projects": rows}, indent=2))
        return 0

    print(f"query={args.query!r}")
    for row in rows:
        print(
            f"project={row['project_slug']} files={row['files']} chunks={row['chunks']} "
            f"indexed_tokens={row['indexed_tokens']} "
            f"retrieved_tokens={row['retrieved_tokens']} "
            f"saved_tokens={row['saved_tokens']} "
            f"savings={row['savings_percent']}%"
        )
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    checks = [
        check_path("root", ROOT_DIR),
        check_command("python", sys.executable),
        check_command("docker", "docker"),
        check_command("ollama", "ollama"),
        check_postgres(),
        check_qdrant(),
        check_ollama_embedding(),
    ]
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'fail'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_install_shims(args: argparse.Namespace) -> int:
    bin_dir = Path(args.bin_dir).expanduser().resolve(strict=False)
    bin_dir.mkdir(parents=True, exist_ok=True)

    commands = {
        "gpc": None,
        "gpc-index": "index",
        "gpc-status": "status",
        "gpc-search": "search",
        "gpc-mcp-http": "mcp-http",
    }
    for command, subcommand in commands.items():
        path = bin_dir / command
        path.write_text(shim_text(subcommand))
        path.chmod(0o755)
        print(f"installed {path}")

    path_value = os.getenv("PATH", "")
    if str(bin_dir) not in path_value.split(os.pathsep):
        print(f"warning={bin_dir} is not currently in PATH")
    return 0


def cmd_install_clients(args: argparse.Namespace) -> int:
    module_args = ["--clients", args.clients]
    if args.dry_run:
        module_args.append("--dry-run")
    if args.validate_only:
        module_args.append("--validate-only")
    if args.skip_smoke:
        module_args.append("--skip-smoke")
    if args.no_backup:
        module_args.append("--no-backup")
    return run_module("scripts.install_mcp_clients", module_args)


def cmd_migrate(args: argparse.Namespace) -> int:
    return run_module("scripts.migrate", [args.migration_command])


def cmd_init_qdrant(args: argparse.Namespace) -> int:
    module_args = ["--reset"] if args.reset else []
    return run_module("scripts.init_qdrant", module_args)


def cmd_mcp_http(args: argparse.Namespace) -> int:
    from gpc.mcp_server import mcp

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = args.path
    print(f"gpc_mcp_http=http://{args.host}:{args.port}{args.path}")
    mcp.run(transport="streamable-http")
    return 0


def cmd_mcp_stdio(_: argparse.Namespace) -> int:
    from gpc.mcp_server import mcp

    mcp.run(transport="stdio")
    return 0


def run_module(module: str, args: Sequence[str]) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    result = subprocess.run([sys.executable, "-m", module, *args], env=env, check=False)
    return result.returncode


def print_index_stats(stats) -> None:
    print(
        f"project={stats.project_slug} mode={stats.discovery_mode} "
        f"files_seen={stats.files_seen} files_indexed={stats.files_indexed} "
        f"files_unchanged={stats.files_unchanged} files_skipped={stats.files_skipped} "
        f"files_failed={stats.files_failed} chunks_written={stats.chunks_written} "
        f"chunks_deleted={stats.chunks_deleted} points_deleted={stats.points_deleted}"
    )
    if stats.skipped_reasons:
        reasons = ", ".join(
            f"{reason}:{count}" for reason, count in sorted(stats.skipped_reasons.items())
        )
        print(f"skipped_reasons={reasons}")
    if stats.errors:
        print("errors:")
        for error in stats.errors:
            print(f"- {error}")


def write_project_config(
    root: Path,
    *,
    slug: str,
    name: str,
    description: str | None,
    aliases: list[str],
    force: bool,
) -> None:
    config_path = root / ".gpc.yaml"
    if config_path.exists() and not force:
        print(f"{config_path}: already exists")
        return

    lines = [
        f"project: {quote_yaml(slug)}",
        f"name: {quote_yaml(name)}",
        f"gpc_root: {quote_yaml(str(ROOT_DIR))}",
        "auto_index: true",
    ]
    if description:
        lines.append(f"description: {quote_yaml(description)}")
    if aliases:
        lines.append("aliases:")
        lines.extend(f"  - {quote_yaml(alias)}" for alias in aliases)
    lines.extend(
        [
            "hooks:",
            *[f"  - {hook}" for hook in HOOKS],
            "",
        ]
    )
    config_path.write_text("\n".join(lines))
    print(f"wrote {config_path}")


def ensure_gitignore_entry(root: Path) -> None:
    gitignore = root / ".gitignore"
    entry = ".gpc/"
    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
        if entry in lines:
            return
        prefix = "\n" if lines else ""
        gitignore.write_text(gitignore.read_text().rstrip() + f"{prefix}\n# GPC runtime data\n{entry}\n")
    else:
        gitignore.write_text(f"# GPC runtime data\n{entry}\n")
    print(f"updated {gitignore}")


def install_git_hooks(root: Path, *, force: bool, background: bool) -> None:
    git_root = git_output(root, "rev-parse", "--show-toplevel")
    if not git_root:
        print(f"{root}: not a Git repository; hooks skipped")
        return

    hook_dir_raw = git_output(root, "rev-parse", "--git-path", "hooks")
    if not hook_dir_raw:
        print(f"{root}: could not resolve Git hooks path; hooks skipped")
        return

    hook_dir = Path(hook_dir_raw)
    if not hook_dir.is_absolute():
        hook_dir = Path(git_root) / hook_dir
    hook_dir.mkdir(parents=True, exist_ok=True)

    for hook in HOOKS:
        hook_path = hook_dir / hook
        content = git_hook_text(hook, background=background)
        if hook_path.exists() and MANAGED_HOOK_MARKER not in hook_path.read_text(errors="ignore"):
            if not force:
                print(f"{hook_path}: existing non-GPC hook kept; use --force to replace")
                continue
        hook_path.write_text(content)
        hook_path.chmod(0o755)
        print(f"installed {hook_path}")


def git_hook_text(hook: str, *, background: bool) -> str:
    python = str(Path(sys.executable))
    body = f"""#!/usr/bin/env bash
# {MANAGED_HOOK_MARKER}: {hook}
set -u

if [ "${{GPC_AUTO_INDEX:-1}}" = "0" ]; then
  exit 0
fi

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$PROJECT_ROOT" || exit 0
mkdir -p .gpc

run_gpc_index() {{
  LOCK_DIR=".gpc/index.lock"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  {{
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gpc auto-index started by {hook}"
    export PYTHONPATH="{ROOT_DIR}:${{PYTHONPATH:-}}"
    "{python}" -m gpc.cli index --no-prune .
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gpc auto-index finished by {hook}"
  }} >> ".gpc/index.log" 2>&1
}}
"""
    if background:
        body += """
( run_gpc_index ) >/dev/null 2>&1 &
exit 0
"""
    else:
        body += """
run_gpc_index
exit 0
"""
    return body


def git_output(root: Path, *args: str) -> str | None:
    if not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def shim_text(subcommand: str | None) -> str:
    python = ROOT_DIR / "venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    module_args = "gpc.cli" if subcommand is None else f"gpc.cli {subcommand}"
    return f"""#!/usr/bin/env bash
set -euo pipefail
GPC_ROOT={json.dumps(str(ROOT_DIR))}
GPC_PYTHON={json.dumps(str(python))}
export PYTHONPATH="$GPC_ROOT:${{PYTHONPATH:-}}"
exec "$GPC_PYTHON" -m {module_args} "$@"
"""


def quote_yaml(value: str) -> str:
    return json.dumps(value)


def check_path(name: str, path: Path) -> tuple[str, bool, str]:
    return name, path.exists(), str(path)


def check_command(name: str, command: str) -> tuple[str, bool, str]:
    if Path(command).exists():
        return name, True, command
    resolved = shutil.which(command)
    return name, bool(resolved), resolved or "not found"


def check_postgres() -> tuple[str, bool, str]:
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            conn.execute("select 1")
        return "postgres", True, POSTGRES_DSN
    except Exception as exc:
        return "postgres", False, str(exc)


def check_qdrant() -> tuple[str, bool, str]:
    try:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        info = client.get_collection(COLLECTION_NAME)
        size = getattr(info.config.params.vectors, "size", None)
        return "qdrant", True, f"{COLLECTION_NAME} size={size}"
    except Exception as exc:
        return "qdrant", False, str(exc)


def check_ollama_embedding() -> tuple[str, bool, str]:
    try:
        return (
            "ollama_embedding",
            True,
            f"{active_embedding_model()} dimensions={embedding_dimension()}",
        )
    except Exception as exc:
        return "ollama_embedding", False, str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
