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
from gpc.registry import (
    add_project_alias,
    consolidate_projects,
    ensure_project,
    list_projects,
    list_repos,
    normalize_slug,
    register_repo,
    resolve_project,
    resolve_repo,
)
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
    init_parser.add_argument(
        "--project",
        dest="parent_project",
        help="Attach this path as a repo of an existing logical project (by slug or alias).",
    )
    init_parser.add_argument(
        "--repo",
        dest="repo_slug",
        help="Repo slug (when used with --project). Defaults to the directory name.",
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

    # Project lifecycle
    project_parser = subparsers.add_parser("project", help="Manage logical projects.")
    project_subs = project_parser.add_subparsers(dest="project_cmd", required=True)

    project_create = project_subs.add_parser(
        "create", help="Create a logical project (virtual root) that can own repos."
    )
    project_create.add_argument("slug")
    project_create.add_argument("--name")
    project_create.add_argument("--description")
    project_create.add_argument("--alias", action="append", default=[])
    project_create.set_defaults(func=cmd_project_create)

    project_list = project_subs.add_parser(
        "list", help="List projects with their repos and aliases."
    )
    project_list.add_argument("--json", action="store_true")
    project_list.set_defaults(func=cmd_project_list)

    project_rename = project_subs.add_parser(
        "rename",
        help="Rename a project end-to-end across Postgres, Qdrant, and Neo4j.",
    )
    project_rename.add_argument("old_slug")
    project_rename.add_argument("new_slug")
    project_rename.add_argument("--new-name")
    project_rename.add_argument(
        "--keep-default-repo",
        action="store_true",
        help="Do not rename a default repo whose slug matches the old project slug.",
    )
    project_rename.add_argument("--yes", action="store_true", required=False)
    project_rename.add_argument("--json", action="store_true")
    project_rename.set_defaults(func=cmd_project_rename)

    project_consolidate = project_subs.add_parser(
        "consolidate",
        help="Fold several standalone projects into one project owning them as repos.",
    )
    project_consolidate.add_argument("--target", required=True, help="Target project slug.")
    project_consolidate.add_argument(
        "--source",
        action="append",
        required=True,
        help="Source project slug. Repeatable.",
    )
    project_consolidate.add_argument("--target-name")
    project_consolidate.add_argument("--target-description")
    project_consolidate.add_argument(
        "--delete-sources",
        action="store_true",
        help="Delete the source project rows after consolidation.",
    )
    project_consolidate.add_argument("--json", action="store_true")
    project_consolidate.set_defaults(func=cmd_project_consolidate)

    # Repo lifecycle
    repo_parser = subparsers.add_parser("repo", help="Manage repositories under a project.")
    repo_subs = repo_parser.add_subparsers(dest="repo_cmd", required=True)

    repo_add = repo_subs.add_parser("add", help="Attach a repo to a project.")
    repo_add.add_argument("project", help="Project slug or alias.")
    repo_add.add_argument("path", help="Repo root path.")
    repo_add.add_argument("--slug")
    repo_add.add_argument("--name")
    repo_add.add_argument("--description")
    repo_add.add_argument(
        "--create-project",
        action="store_true",
        help="Create the project if it does not exist yet.",
    )
    repo_add.set_defaults(func=cmd_repo_add)

    repo_list_p = repo_subs.add_parser("list", help="List repos.")
    repo_list_p.add_argument("--project")
    repo_list_p.add_argument("--json", action="store_true")
    repo_list_p.set_defaults(func=cmd_repo_list)

    repo_remove = repo_subs.add_parser("remove", help="Detach a repo from its project.")
    repo_remove.add_argument("project", help="Project slug or alias.")
    repo_remove.add_argument("repo", help="Repo slug.")
    repo_remove.add_argument("--yes", action="store_true")
    repo_remove.set_defaults(func=cmd_repo_remove)

    # Graph / backend reset
    graph_reset_parser = subparsers.add_parser(
        "graph-reset",
        help="Wipe the Neo4j projection and optionally rebuild it from Postgres.",
    )
    graph_reset_parser.add_argument("--project", help="Restrict to a single project slug.")
    graph_reset_parser.add_argument("--yes", action="store_true", required=False)
    graph_reset_parser.add_argument(
        "--rebuild", action="store_true", help="Re-run project_graph_to_neo4j and bridging after the wipe."
    )
    graph_reset_parser.set_defaults(func=cmd_graph_reset)

    reset_parser = subparsers.add_parser(
        "reset",
        help="NUCLEAR: drop Postgres tables, wipe Neo4j, and recreate Qdrant.",
    )
    reset_parser.add_argument("--yes", action="store_true", required=False)
    reset_parser.add_argument("--skip-postgres", action="store_true")
    reset_parser.add_argument("--skip-neo4j", action="store_true")
    reset_parser.add_argument("--skip-qdrant", action="store_true")
    reset_parser.set_defaults(func=cmd_reset)

    metrics_parser = subparsers.add_parser(
        "metrics",
        help="Longitudinal metrics for the graph (drives Fase 3 drift detection).",
    )
    metrics_subs = metrics_parser.add_subparsers(dest="metrics_cmd", required=True)

    metrics_collect = metrics_subs.add_parser(
        "collect",
        help="Record a snapshot of graph metrics for a project.",
    )
    metrics_collect.add_argument("--project", required=True)
    metrics_collect.add_argument(
        "--source",
        default="manual",
        choices=("manual", "snapshot"),
        help="Trigger label stored with the snapshot.",
    )
    metrics_collect.add_argument("--json", action="store_true")
    metrics_collect.set_defaults(func=cmd_metrics_collect)

    metrics_list = metrics_subs.add_parser(
        "list",
        help="List recent metric snapshots.",
    )
    metrics_list.add_argument("--project")
    metrics_list.add_argument("--limit", type=int, default=20)
    metrics_list.add_argument("--json", action="store_true")
    metrics_list.set_defaults(func=cmd_metrics_list)

    bridge_parser = subparsers.add_parser(
        "graph-bridge",
        help="Create CROSS_REPO_BRIDGE edges between GraphifyNodes of a project.",
    )
    bridge_parser.add_argument(
        "--project",
        help="Project slug. If omitted, bridges every GraphifyProject in Neo4j.",
    )
    bridge_parser.add_argument(
        "--rule",
        dest="rules",
        action="append",
        choices=(
            "content_hash",
            "same_source_file",
            "same_code_symbol",
            "same_generic_symbol",
        ),
        help="Bridging rule to apply. Repeatable. Default: content_hash + same_source_file + same_code_symbol.",
    )
    bridge_parser.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Shortcut: also apply the same_generic_symbol rule (emits AMBIGUOUS edges).",
    )
    bridge_parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete existing CROSS_REPO_BRIDGE edges for the project before writing.",
    )
    bridge_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human output.",
    )
    bridge_parser.set_defaults(func=cmd_graph_bridge)

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

    parent_project = getattr(args, "parent_project", None)
    repo_slug = normalize_slug(args.repo_slug) if getattr(args, "repo_slug", None) else None

    if parent_project:
        # New mode: attach this directory as a repo of an existing project.
        repo = register_repo(
            parent_project,
            root,
            slug=repo_slug or root.name,
            name=args.name,
            description=args.description,
            create_project_if_missing=False,
        )
        project_slug_for_index = repo["project_slug"]
        repo_slug_for_index = repo["slug"]
        description = args.description

        if not args.no_gitignore:
            ensure_gitignore_entry(root)
        if not args.no_hooks:
            install_git_hooks(root, force=args.force, background=not args.foreground_hooks)
        write_repo_config(
            root,
            project_slug=project_slug_for_index,
            repo_slug=repo_slug_for_index,
            force=args.force,
        )

        if args.no_index:
            print(
                f"repo_registered project={project_slug_for_index} "
                f"repo={repo_slug_for_index} path={root}"
            )
            return 0

        stats = index_project_path(
            root,
            slug=project_slug_for_index,
            name=args.name,
            description=description,
            options=IndexOptions(),
            repo_slug=repo_slug_for_index,
        )
        print_index_stats(stats)
        return 1 if stats.files_failed else 0

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


def write_repo_config(
    root: Path,
    *,
    project_slug: str,
    repo_slug: str,
    force: bool,
) -> None:
    config_path = root / ".gpc.yaml"
    if config_path.exists() and not force:
        return
    config_path.write_text(
        f"project: {project_slug}\nrepo: {repo_slug}\n",
        encoding="utf-8",
    )


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


def cmd_project_create(args: argparse.Namespace) -> int:
    project = ensure_project(
        slug=args.slug,
        name=args.name,
        description=args.description,
        aliases=args.alias or None,
    )
    print(
        f"project={project['slug']} name={project['name']} "
        f"aliases={','.join(project.get('aliases', []) or [])}"
    )
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    projects = list_projects()
    repos_by_project: dict[str, list[dict]] = {}
    for repo in list_repos():
        repos_by_project.setdefault(repo["project_slug"], []).append(repo)

    if args.json:
        payload = [
            {
                "slug": p["slug"],
                "name": p["name"],
                "aliases": p.get("aliases", []),
                "root_path": p.get("root_path"),
                "repos": [
                    {
                        "slug": r["slug"],
                        "name": r["name"],
                        "root_path": r["root_path"],
                    }
                    for r in repos_by_project.get(p["slug"], [])
                ],
            }
            for p in projects
        ]
        print(json.dumps(payload, default=str, indent=2))
        return 0

    if not projects:
        print("No projects registered. Create one with `gpc project create <slug>`.")
        return 0

    for project in projects:
        repos = repos_by_project.get(project["slug"], [])
        aliases = ",".join(project.get("aliases") or [])
        print(f"project={project['slug']} name={project['name']} aliases={aliases}")
        for repo in repos:
            print(f"  repo={repo['slug']} path={repo['root_path']}")
        if not repos:
            print("  (no repos)")
    return 0


def cmd_project_rename(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from gpc.project_rename import ProjectRenameError, rename_project

    if not args.yes:
        print(
            "Refuse: pass --yes to confirm. `gpc project rename` mutates "
            "Postgres, Qdrant payloads, and the Neo4j projection for the "
            "given project. The old slug is preserved as an alias so "
            "existing references keep resolving.",
            file=sys.stderr,
        )
        return 2

    try:
        stats = rename_project(
            args.old_slug,
            args.new_slug,
            new_name=args.new_name,
            rename_default_repo=not args.keep_default_repo,
        )
    except ProjectRenameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(stats), default=str, indent=2))
        return 0

    print(
        f"renamed old_slug={stats.old_slug} new_slug={stats.new_slug} "
        f"name={stats.project_name!r} "
        f"aliases_added={','.join(stats.aliases_added) or '-'} "
        f"repos_renamed={len(stats.repos_renamed)} "
        f"self_metrics_rows={stats.self_metrics_rows} "
        f"qdrant_points={stats.qdrant_points_updated} "
        f"neo4j_nodes={stats.neo4j_nodes_updated} "
        f"neo4j_edges={stats.neo4j_edges_updated}"
    )
    for warning in stats.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


def cmd_project_consolidate(args: argparse.Namespace) -> int:
    stats = consolidate_projects(
        args.target,
        args.source,
        target_name=args.target_name,
        target_description=args.target_description,
        delete_source_projects=args.delete_sources,
    )
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(
        f"consolidated target={stats['target']} "
        f"sources={','.join(stats['sources'])} "
        f"repos_created={stats['repos_created']} "
        f"files_moved={stats['files_moved']} "
        f"chunks_moved={stats['chunks_moved']} "
        f"entities_moved={stats['entities_moved']} "
        f"relations_moved={stats['relations_moved']} "
        f"decisions_moved={stats['decisions_moved']} "
        f"aliases_moved={stats['aliases_moved']} "
        f"source_projects_deleted={stats['source_projects_deleted']}"
    )
    return 0


def cmd_repo_add(args: argparse.Namespace) -> int:
    repo = register_repo(
        args.project,
        args.path,
        slug=args.slug,
        name=args.name,
        description=args.description,
        create_project_if_missing=args.create_project,
    )
    print(
        f"repo={repo['slug']} project={repo['project_slug']} path={repo['root_path']}"
    )
    return 0


def cmd_repo_list(args: argparse.Namespace) -> int:
    rows = list_repos(args.project)
    if args.json:
        print(json.dumps(
            [
                {
                    "project": r["project_slug"],
                    "repo": r["slug"],
                    "name": r["name"],
                    "root_path": r["root_path"],
                    "description": r.get("description"),
                }
                for r in rows
            ],
            default=str,
            indent=2,
        ))
        return 0
    if not rows:
        print("No repos registered.")
        return 0
    for r in rows:
        print(f"{r['project_slug']:<25} {r['slug']:<25} {r['root_path']}")
    return 0


def cmd_repo_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refuse: pass --yes to confirm. Files and chunks will keep their "
              "project_id but lose their repo_id (set to NULL).", file=sys.stderr)
        return 2
    import psycopg as _psycopg  # noqa: F401
    from gpc.registry import connect
    project_slug = normalize_slug(args.project)
    repo_slug = normalize_slug(args.repo)
    with connect() as conn:
        project = conn.execute(
            "select id from gpc_projects where slug = %s",
            (project_slug,),
        ).fetchone()
        if not project:
            print(f"Project not found: {project_slug}", file=sys.stderr)
            return 1
        deleted = conn.execute(
            "delete from gpc_repos where project_id = %s and slug = %s",
            (project["id"], repo_slug),
        ).rowcount or 0
    print(f"repo_removed project={project_slug} repo={repo_slug} deleted={deleted}")
    return 0


def cmd_graph_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "Refuse: pass --yes to confirm. This wipes the Neo4j projection "
            "for the given scope. Postgres and Qdrant are not touched.",
            file=sys.stderr,
        )
        return 2
    from gpc.graph_reset import reset_and_rebuild, reset_neo4j

    if args.rebuild:
        stats = reset_and_rebuild(
            project_slug=args.project,
            rebuild_gpc=True,
            rebuild_graphify_bridges=True,
        )
    else:
        stats = reset_neo4j(project_slug=args.project)

    print(
        f"neo4j_nodes_deleted={stats.neo4j_nodes_deleted} "
        f"neo4j_relationships_deleted={stats.neo4j_relationships_deleted} "
        f"gpc_rebuilt={stats.gpc_rebuilt} "
        f"graphify_projects_bridged={stats.graphify_projects_bridged} "
        f"bridges_written={stats.bridges_written}"
    )
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "Refuse: pass --yes to confirm. `gpc reset` drops every gpc_* "
            "table, wipes the Neo4j projection, and recreates the Qdrant "
            "collection. Re-indexing is required afterwards.",
            file=sys.stderr,
        )
        return 2
    from gpc.reset import reset_all

    report = reset_all(
        run_module=run_module,
        postgres=not args.skip_postgres,
        neo4j=not args.skip_neo4j,
        qdrant=not args.skip_qdrant,
    )
    print(
        f"postgres_dropped={len(report.postgres_tables_dropped)} "
        f"postgres_migrations_applied={','.join(report.postgres_migrations_applied) or '-'} "
        f"neo4j_nodes_deleted={report.neo4j_nodes_deleted} "
        f"neo4j_relationships_deleted={report.neo4j_relationships_deleted} "
        f"qdrant_recreated={report.qdrant_collection_recreated}"
    )
    return 0


def cmd_metrics_collect(args: argparse.Namespace) -> int:
    from gpc.self_metrics import collect_metrics

    result = collect_metrics(project_slug=args.project, source=args.source)
    if args.json:
        print(
            json.dumps(
                {
                    "id": result.id,
                    "project_slug": result.project_slug,
                    "source": result.source,
                    "counts": result.counts,
                    "god_nodes_top10": result.god_nodes_top10,
                    "metadata": result.metadata,
                },
                default=str,
                indent=2,
            )
        )
        return 0
    print(
        f"snapshot={result.id} project={result.project_slug} source={result.source}"
    )
    for key in (
        "files_count",
        "chunks_count",
        "entities_count",
        "relations_count",
        "graphify_nodes",
        "graphify_edges_same_repo",
        "graphify_edges_cross_repo",
        "cross_repo_bridges",
        "weakly_connected_nodes",
        "community_count",
    ):
        value = result.counts.get(key)
        if value is not None:
            print(f"  {key}={value}")
    return 0


def cmd_metrics_list(args: argparse.Namespace) -> int:
    from gpc.self_metrics import list_snapshots

    rows = list_snapshots(project_slug=args.project, limit=args.limit)
    if args.json:
        print(json.dumps(rows, default=str, indent=2))
        return 0
    if not rows:
        print("No metric snapshots recorded yet.")
        return 0
    for row in rows:
        print(
            f"{row['collected_at']}  {row['project_slug']:<25} {row['source']:<14} "
            f"files={row.get('files_count')} chunks={row.get('chunks_count')} "
            f"nodes={row.get('graphify_nodes')} bridges={row.get('cross_repo_bridges')} "
            f"weak={row.get('weakly_connected_nodes')} comms={row.get('community_count')}"
        )
    return 0


def cmd_graph_bridge(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from gpc.cross_repo import (
        ALL_RULES,
        DEFAULT_RULES,
        RULE_SAME_GENERIC_SYMBOL,
        build_bridges,
        build_bridges_all_projects,
    )

    rules = tuple(args.rules) if args.rules else DEFAULT_RULES
    if args.include_ambiguous and RULE_SAME_GENERIC_SYMBOL not in rules:
        rules = rules + (RULE_SAME_GENERIC_SYMBOL,)
    for rule in rules:
        if rule not in ALL_RULES:
            print(f"Unknown bridging rule: {rule}", file=sys.stderr)
            return 2

    if args.project:
        results = [build_bridges(args.project, rules=rules, clear_existing=args.clear)]
    else:
        results = build_bridges_all_projects(rules=rules, clear_existing=args.clear)

    if args.json:
        print(json.dumps([asdict(r) for r in results], default=list, indent=2))
        return 0

    if not results:
        print("No GraphifyProject nodes found in Neo4j — nothing to bridge.")
        return 0

    for stats in results:
        print(
            f"project={stats.project_slug} repos={stats.repos} "
            f"same_source_file={stats.pairs_same_source_file} "
            f"same_code_symbol={stats.pairs_same_code_symbol} "
            f"same_generic_symbol={stats.pairs_same_generic_symbol} "
            f"edges_written={stats.edges_written} "
            f"edges_deleted={stats.edges_deleted} "
            f"rules={','.join(stats.rules_applied)}"
        )
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
