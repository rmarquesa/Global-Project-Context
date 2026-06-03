# Project Layout

GPC exposes a product-level CLI and keeps implementation details in a Python
package. This document maps the CLI surface to the underlying modules and
explains where each piece of behaviour lives.

## Public Commands

Installed by `./install.sh` into `~/.local/bin` (override with `--bin-dir`):

| Command | Purpose |
|---|---|
| `gpc` | Main CLI: project init, doctor, verify, migrate, install-clients, token-savings, graph sync/bridge. |
| `gpc-index` | Index a project root manually. |
| `gpc-status` | Inspect indexed files, chunks, Qdrant points and recent runs. |
| `gpc-search` | Run a semantic search over an indexed project. |
| `gpc-mcp-http` | Run the MCP server over local streamable HTTP. |

Common invocations:

```bash
gpc doctor
gpc verify --project project-slug
gpc verify --project project-slug --quick --json
gpc init . --slug project-slug --name "Project Name"
gpc init /path/to/repo --project project-slug --repo repo-slug --name "Repo Name"
gpc-index /path/to/project --slug project-slug
gpc-index /path/to/repo --project project-slug --repo repo-slug
gpc-status --project project-slug
gpc-status --project project-slug --staleness
gpc stale --project project-slug --json
gpc-search "query" --project project-slug
gpc token-savings "query" --project project-slug
gpc eval-search --project project-slug --fixture tests/fixtures/search_eval_gpc.yml --k 5
gpc context-pack "MCP architecture" --project project-slug --output /tmp/gpc-context.md
gpc health-report --project project-slug --json
gpc mcp-usage --project project-slug --since 24h --json
gpc maintenance doctor --project project-slug --json
gpc graph-sync /path/to/repo --project project-slug --repo repo-slug
gpc graph-bridge --project project-slug
gpc install-clients
gpc-mcp-http --host 127.0.0.1 --port 8765
```

## Python Package

The reusable implementation lives under `gpc/`.

| Module | Responsibility |
|---|---|
| `gpc/cli.py` | Public command surface; hook installer and shim installer. |
| `gpc/config.py` | Central configuration, `.env` loading, and credential guard. |
| `gpc/db.py` | Shared long-lived backing-store handles: pooled Postgres connection, singleton Qdrant client, singleton Neo4j driver. |
| `gpc/embeddings.py` | Local Ollama embedding adapter with a per-query LRU cache. |
| `gpc/registry.py` | Project / source registry and resolver. |
| `gpc/indexer.py` | File discovery, chunking, embeddings, Postgres and Qdrant writes. |
| `gpc/search.py` | Semantic search over Qdrant with Postgres hydration. |
| `gpc/search_eval.py` | Search-quality fixture runner and recall metrics. |
| `gpc/context_pack.py` | Cited Markdown context-pack rendering and CLI output helpers. |
| `gpc/health_report.py` | Operator health rollup over verify, index, graph, drift and MCP usage signals. |
| `gpc/maintenance.py` | Dry-run maintenance diagnostics for read-model consistency. |
| `gpc/status.py` | Shared index status helper used by CLI and MCP. |
| `gpc/staleness.py` | Git/index/Graphify freshness detector used by `gpc stale` and `gpc verify`. |
| `gpc/verify.py` | Project health verification across registry, index, services, and graph state. |
| `gpc/graph.py` | Postgres → Neo4j projection helpers. |
| `gpc/graphify_sync.py` | `graphify-out/graph.json` → Neo4j Graphify projection sync. |
| `gpc/token_economy.py` | Token-savings estimator. |
| `gpc/mcp_server.py` | Read-only MCP tool server. |

The root file `gpc_mcp_server.py` is a thin wrapper that exposes the MCP
server entrypoint at a stable path. AI client configurations point at this
file.

## Internal Scripts

The `scripts/` directory backs the public CLI and the installer. These
modules are not part of the public surface — invoke them through `gpc`,
`gpc-index`, `gpc-status` and `gpc-search` rather than calling them directly.

| Script | Purpose |
|---|---|
| `scripts/install_mcp_clients.py` | Installs and validates GPC's MCP server in supported AI clients. |
| `scripts/init_qdrant.py` | Creates or resets the `gpc_memory` Qdrant collection. |
| `scripts/create_collection.py` | Backwards-compatible wrapper around `init_qdrant.py`. |
| `scripts/migrate.py` | Applies and validates Postgres migrations. |
| `scripts/estimate_token_savings.py` | CLI driver behind `gpc token-savings`. |

## Migrations

SQL migrations live under `migrations/` and are applied in order by
`gpc migrate up`. The migration runner stores progress in
`gpc_schema_migrations`.

| File | Adds |
|---|---|
| `migrations/0001_initial_schema.sql` | Core tables: projects, files, chunks, runs. |
| `migrations/0002_project_aliases_sources.sql` | Project aliases and external sources. |
| `migrations/0003_graph_projections.sql` | Neo4j projection run tracking. |
| `migrations/0004_gpc_repos.sql` | Logical project / repository registry. |
| `migrations/0005_repo_scoped_uniqueness.sql` | Repo-scoped file and chunk constraints. |
| `migrations/0006_mcp_call_log.sql` | MCP tool-call audit log. |
| `migrations/0007_gpc_self_metrics.sql` | Self-metrics snapshots for index and graph runs. |
| `migrations/0008_token_savings_samples.sql` | Persisted token-economy samples for Grafana. |
| `migrations/0009_drift_signals.sql` | Rule-based graph drift signals. |

## Examples

Reference Git hooks live under `examples/hooks/`.

| File | Purpose |
|---|---|
| `examples/hooks/graphify-neo4j-post-commit.sh` | Refreshes a repository's Graphify graph and pushes it into the shared Neo4j database. See [graphify.md](graphify.md). |

## Smoke Tests

End-to-end tests live under `tests/smoke/`. They require the Docker services
and Ollama to be running.

```bash
./scripts/run_smoke_tests.sh
```

The wrapper is the source of truth for smoke-test order; it refreshes the Qdrant
bootstrap seed before search assertions, validates graph projection/querying,
then verifies the local `graphify-out/graph.json` for this repo is visible
through the Neo4j-backed graph summary surface before running the remaining
live-service checks.

See [CONTRIBUTING.md](../CONTRIBUTING.md#testing) for guidance on when to add
new smoke coverage.

## Root Files

The repository root stays focused on project-level assets:

| File | Purpose |
|---|---|
| `README.md` | Project overview and quickstart. |
| `LICENSE` | AGPL-3.0-or-later. |
| `NOTICE.md` | License intent and limits. |
| `CONTRIBUTING.md` | Contributor guide. |
| `SECURITY.md` | Security policy. |
| `Dockerfile` | Optional image for HTTP MCP. |
| `docker-compose.yaml` | Local Postgres, Qdrant, Neo4j, optional HTTP MCP and optional Grafana. |
| `.env.example` | Local service configuration template. |
| `install.sh` | One-shot installer. |
| `gpc_mcp_server.py` | Stable MCP entrypoint wrapper. |
| `mcp_config.example.json` | MCP client configuration template. |
| `pyproject.toml` | Packaging metadata and canonical Black / Ruff / mypy / pytest config. |
| `requirements.txt` | Version-bounded runtime dependencies. |
| `requirements-dev.txt` | Dev/test/lint dependencies (black, ruff, mypy, pytest). |
| `CHANGELOG.md` | Release notes. |
