# Project Layout

GPC exposes a product-level CLI and keeps implementation details in a Python
package. This document maps the CLI surface to the underlying modules and
explains where each piece of behaviour lives.

## Public Commands

Installed by `./install.sh` into `~/.local/bin` (override with `--bin-dir`):

| Command | Purpose |
|---|---|
| `gpc` | Main CLI: project init, doctor, migrate, install-clients, token-savings. |
| `gpc-index` | Index a project root manually. |
| `gpc-status` | Inspect indexed files, chunks, Qdrant points and recent runs. |
| `gpc-search` | Run a semantic search over an indexed project. |
| `gpc-mcp-http` | Run the MCP server over local streamable HTTP. |

Common invocations:

```bash
gpc doctor
gpc init . --slug project-slug --name "Project Name"
gpc-index /path/to/project --slug project-slug
gpc-status --project project-slug
gpc-search "query" --project project-slug
gpc token-savings "query" --project project-slug
gpc install-clients
gpc-mcp-http --host 127.0.0.1 --port 8765
```

## Python Package

The reusable implementation lives under `gpc/`.

| Module | Responsibility |
|---|---|
| `gpc/cli.py` | Public command surface; hook installer and shim installer. |
| `gpc/config.py` | Central configuration and `.env` loading. |
| `gpc/embeddings.py` | Local Ollama embedding adapter. |
| `gpc/registry.py` | Project / source registry and resolver. |
| `gpc/indexer.py` | File discovery, chunking, embeddings, Postgres and Qdrant writes. |
| `gpc/search.py` | Semantic search over Qdrant with Postgres hydration. |
| `gpc/status.py` | Shared index status helper used by CLI and MCP. |
| `gpc/graph.py` | Postgres → Neo4j projection helpers. |
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

## Examples

Reference Git hooks live under `examples/hooks/`.

| File | Purpose |
|---|---|
| `examples/hooks/graphify-neo4j-post-commit.sh` | Refreshes a repository's Graphify graph and pushes it into the shared Neo4j database. See [graphify.md](graphify.md). |

## Smoke Tests

End-to-end tests live under `tests/smoke/`. They require the Docker services
and Ollama to be running.

```bash
./venv/bin/python -m tests.smoke.embedding_smoke_test
./venv/bin/python -m tests.smoke.search_test
./venv/bin/python -m tests.smoke.registry_smoke_test
./venv/bin/python -m tests.smoke.graph_projection_smoke_test
./venv/bin/python -m tests.smoke.mcp_smoke_test
```

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
| `docker-compose.yaml` | Local Postgres, Qdrant, Neo4j and optional HTTP MCP. |
| `.env.example` | Local service configuration template. |
| `install.sh` | One-shot installer. |
| `gpc_mcp_server.py` | Stable MCP entrypoint wrapper. |
| `mcp_config.example.json` | MCP client configuration template. |
| `requirements.txt` | Python dependencies. |
