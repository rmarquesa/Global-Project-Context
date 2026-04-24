# Operations

This guide covers the day-to-day operational path for GPC: installing,
validating, indexing projects, and resetting state when needed.

## Prerequisites

- Python 3.12 or newer.
- Docker and Docker Compose.
- A local [Ollama](https://ollama.com) installation reachable at
  `http://localhost:11434`.
- The default Ollama embedding model: `nomic-embed-text:latest`.

## Install

```bash
./install.sh
```

The installer performs the following steps:

1. Creates the Python virtual environment under `./venv`.
2. Installs Python dependencies from `requirements.txt`.
3. Starts the local Postgres, Qdrant and Neo4j containers via
   `docker compose up -d`.
4. Pulls the default Ollama embedding model if it is missing.
5. Applies pending Postgres migrations (`gpc migrate up`).
6. Initializes the Qdrant `gpc_memory` collection.
7. Installs `gpc`, `gpc-index`, `gpc-status`, `gpc-search` and `gpc-mcp-http`
   shims into your `PATH`.
8. Installs the GPC MCP server into every supported AI client found on the
   machine.
9. Runs a smoke validation pass.

### Common options

| Flag | Effect |
|---|---|
| `--skip-clients` | Skip step 8. Use during development to avoid touching real client config. |
| `--clients codex,claude-code,copilot` | Limit step 8 to specific clients. |
| `--bin-dir ~/.local/bin` | Override the install location of the CLI shims. |

## Validate

```bash
gpc doctor
gpc install-clients --validate-only
```

For a full smoke pass:

```bash
./venv/bin/python -m tests.smoke.embedding_smoke_test
./venv/bin/python -m tests.smoke.search_test
./venv/bin/python -m tests.smoke.registry_smoke_test
./venv/bin/python -m tests.smoke.graph_projection_smoke_test
./venv/bin/python -m tests.smoke.mcp_smoke_test
```

Each test exits non-zero on failure and prints which service was unreachable.

## Index a Project

Inside any Git repository:

```bash
cd /path/to/project
gpc init . --slug project-slug --name "Project Name"
```

Use this `--slug` form only when the checkout is the whole project. For a
repository inside a logical multi-repo project, initialize it with the parent
project and a repo slug:

```bash
gpc init . --project project-slug --repo repo-slug --name "Repo Name"
```

This:

- Creates `.gpc.yaml` with the project identity.
- Installs `post-commit`, `post-merge` and `post-checkout` hooks.
- Adds `.gpc/` to the project's `.gitignore`.
- Runs the first full index.

`--slug` is optional; when omitted GPC derives it from `--name` (lowercase,
dashes for spaces). Pass it explicitly when you want a stable identifier
across machines.

After init, the hooks reindex incrementally in the background after every
commit, merge and checkout. Logs go to `.gpc/index.log`.

### Manual indexing

When you want immediate control without going through Git:

```bash
gpc-index /path/to/project --slug project-slug --name "Project Name"
```

The indexer hashes file contents and skips unchanged files. Useful flags:

| Flag | Effect |
|---|---|
| `--limit-files N` | Index at most N files (smoke tests). Disables pruning. |
| `--no-prune` | Do not remove records for files that disappeared. |
| `--force` | Reindex even when the content hash is unchanged. |
| `--reset` | Drop project chunks and reindex from scratch. |
| `--include-unknown-text` | Index text files with unrecognized extensions. |
| `--follow-symlinks` | Follow symlinks during discovery. |

### Status and search

```bash
gpc-status --project project-slug
gpc-search "how does authentication work?" --project project-slug
gpc token-savings "how does authentication work?" --project project-slug
```

`gpc-status` reports indexed file count, chunk count, Qdrant point count and
the last few index runs. Use it to confirm coverage before relying on
retrieval.

## Reset Local State

Drop everything and rebuild from scratch:

```bash
docker compose down -v --remove-orphans
docker compose up -d
gpc migrate up
gpc init-qdrant --reset
```

This removes the Postgres, Qdrant and Neo4j volumes. Project indexes then need
to be re-run with `gpc-index --reset` for each project.

## Optional: Consolidate Repository Graphs With Graphify

GPC can consolidate per-repository [Graphify](https://github.com/rmarquesa/Graphify)
graphs into the bundled Neo4j Community database, separated by `project_slug`
and `repo_slug`. Setup steps, environment variables and Cypher examples live
in [graphify.md](graphify.md).

## See Also

- [Architecture](architecture.md) — components and data flow.
- [Automation](automation.md) — CLI shims and Git hooks.
- [MCP clients](mcp-clients.md) — AI client setup and tool reference.
- [Token economy](token-economy.md) — what `gpc token-savings` reports.
