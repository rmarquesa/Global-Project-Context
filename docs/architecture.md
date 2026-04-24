# Architecture

GPC is a local persistent context layer for AI coding tools. It separates
durable records, semantic retrieval, graph traversal and client access behind a
single read-only MCP server.

## Overview

```text
AI client (Codex / Claude / OpenClaw / Gemini / Copilot)
        │
        │ MCP stdio or HTTP
        ▼
GPC MCP server (gpc/mcp_server.py)
        │
        ├── Postgres (pgvector image) — source of truth
        ├── Qdrant                    — semantic vector store
        ├── Ollama                    — local embedding generator
        └── Neo4j (Community)         — graph projections
```

Indexing and embedding writes go through the CLI or per-project Git hooks.
The MCP server itself is read-only.

## Components

### Postgres — source of truth

GPC ships with the official Postgres image extended with `pgvector`. It owns
the canonical state of the system:

- registered projects and aliases;
- registered sources (e.g. an Obsidian vault) and their links to projects;
- indexed files and their content hashes;
- chunks and chunk metadata;
- index runs (status, timing, counts);
- entities, relations and decisions;
- Neo4j projection run history.

If Postgres is intact, the rest of the stack can be rebuilt.

### Qdrant — semantic vector store

Qdrant stores embeddings for searchable chunks. It is queried by the MCP
server and by the `gpc-search` CLI. It does not own canonical metadata — every
vector carries a payload that points back to a Postgres chunk row.

| Setting | Value |
|---|---|
| Collection | `gpc_memory` |
| Distance | Cosine |
| Vector size | 768 (default with `nomic-embed-text:latest`) |

Recreate the collection with `gpc init-qdrant --reset` when the embedding
model changes.

### Ollama — local embeddings

Ollama runs on the host machine (not inside the GPC compose). The default
embedding model is `nomic-embed-text:latest`, producing 768-dimensional
vectors. Project text is therefore not sent to a remote API by default.

Override via `GPC_OLLAMA_EMBEDDING_MODEL`. Set `GPC_VECTOR_SIZE=0` to detect
the dimension automatically from the active model. Do not mix vectors from
different models in the same Qdrant collection.

### Neo4j — graph projections (optional)

Neo4j is a rebuildable read model. Two distinct subgraphs can coexist in the
single Community database:

1. **GPC projection** — derived from Postgres `gpc_entities` and
   `gpc_relations`. Labels: `GPCProject`, `GPCEntity`. Edges: `OWNS_ENTITY`,
   `GPC_RELATION`. Driven by `gpc/graph.py`.

2. **Graphify projection** — pushed by the Graphify post-commit hook from each
   repository's `graphify-out/graph.json`. Labels: `GraphifyProject`,
   `GraphifyRepo`, `GraphifyNode`. Edges: `HAS_REPO`, `HAS_GRAPH_NODE`,
   `GRAPHIFY_RELATION`. Separation by `project_slug` and `repo_slug`.

Both subgraphs are namespaced and do not interfere with each other. Either can
be cleared and rebuilt without affecting the other or Postgres.

```text
(:GraphifyProject {slug: "commerce"})
  -[:HAS_REPO]-> (:GraphifyRepo {slug: "commerce-api"})
                   -[:HAS_GRAPH_NODE]-> (:GraphifyNode { … })
                                          -[:GRAPHIFY_RELATION]-> (:GraphifyNode { … })
```

Neo4j Community supports a single user database; GPC works around this by
namespacing nodes with `project_slug` plus `repo_slug` rather than relying on
multi-database separation.

### Project + repo model

A GPC project is a logical unit (e.g. `alugafacil`). Each project owns one or
more repositories via `gpc_repos`, and every `gpc_files` / `gpc_chunks` row
carries both a `project_id` and a `repo_id`. The Neo4j projection mirrors this
hierarchy:

```text
(:GPCProject {slug: "alugafacil"})
  -[:OWNS_REPO]-> (:GPCRepo {slug: "workers-gateway"})
  -[:OWNS_REPO]-> (:GPCRepo {slug: "workers-users"})
  -[:OWNS_REPO]-> (:GPCRepo {slug: "web"})
  -[:OWNS_ENTITY]-> (:GPCEntity { ... })
```

Typical bootstrapping of a multi-repo project:

```bash
gpc project create alugafacil --name "AlugaFácil"
gpc init /path/to/workers-gateway --project alugafacil --repo workers-gateway
gpc init /path/to/workers-users   --project alugafacil --repo workers-users
gpc init /path/to/web             --project alugafacil --repo web
```

Existing standalone projects can be folded into a shared parent with
`gpc project consolidate --target <slug> --source <slug> [--source ...]`.
Aliases of the source projects are re-pointed to the target, and files,
chunks, entities, relations and decisions are moved atomically.

A project's slug can be corrected with
`gpc project rename <old_slug> <new_slug> [--new-name <name>] --yes`. The
rename touches Postgres (`gpc_projects`, `gpc_repos`, `gpc_self_metrics`,
`gpc_project_aliases`), Qdrant point payloads, and every slug-bearing
Neo4j label in one shot. The old slug is preserved as an alias so
existing `.gpc.yaml` files, links and prior runs keep resolving to the
renamed project.

Neo4j remains a rebuildable read model; Postgres is the source of truth. Use
`gpc graph-reset --yes [--rebuild]` to wipe the projection and rebuild it
from Postgres, and `gpc reset --yes` for a nuclear reset across all three
backends (Postgres schema drop + re-migrate, Neo4j wipe, Qdrant recreate).

### Cross-repo bridging

Graphify runs inside each repository in isolation. It never sees two repos at
once, so it cannot extract relationships that span them. To reconstruct those
relationships without forcing a monorepo-style unified run, `gpc/cross_repo.py`
adds `CROSS_REPO_BRIDGE` edges at the Neo4j layer *after* each projection.

Bridges are scoped strictly by `project_slug` and tagged with confidence so
downstream clients can filter noise. Three rules ship today:

| Rule | Confidence | Signal |
|---|---|---|
| `same_source_file` | `INFERRED` (0.90) | Two `GraphifyNode` in different repos share the same relative `source_file`. Strong indicator of vendored code or contract files. |
| `same_code_symbol` | `INFERRED` (0.75) | Same `label` + `file_type=code`, label is distinctive (not `main`, `index`, `config`, etc.). |
| `same_generic_symbol` | `AMBIGUOUS` (0.30) | Same label but generic. Opt-in via `--include-ambiguous`. |

Bridging is idempotent (`MERGE` on `(a, b, rule)`), runs after every
post-commit projection when `GPC_GRAPHIFY_BRIDGE_AFTER=1` (default), and can
be invoked manually with `gpc graph-bridge --project <slug>` or across all
projects with `gpc graph-bridge` (no slug argument).

## Data Flow

### Index path (writes)

```text
project files
  │
  ▼ git ls-files --cached --others --exclude-standard
file discovery
  │
  ▼ skip ignored, binary, oversized, secret-like
chunker
  │
  ▼ Ollama
embedder
  │
  ├── Postgres: gpc_files, gpc_chunks (canonical)
  └── Qdrant:   gpc_memory points (vectors + payload pointer)
```

### Retrieval path (reads)

```text
AI client query
  │
  ▼ MCP gpc.search / gpc.context
embed query (Ollama)
  │
  ▼ filter by project
Qdrant nearest neighbours
  │
  ▼ hydrate from Postgres
bounded context block (max_chunks, max_chars)
```

## Project Resolution

The MCP server resolves a project in this order:

1. Explicit `project` slug or alias passed by the client.
2. Caller `cwd` matched against registered project roots (longest-prefix).
3. `.gpc.yaml` discovered by walking up from the working directory.
4. `GPC_CALLER_CWD` environment variable.
5. The MCP process's own working directory (last resort).

Passing `project` or `cwd` explicitly is the most reliable cross-client mode.

## Automation Boundary

The MCP surface is intentionally read-only. Writes happen through:

- `gpc-index` — manual indexing.
- `gpc init` — registers the project, installs Git hooks, runs the first index.
- `post-commit`, `post-merge`, `post-checkout` Git hooks — incremental
  reindexing in the background, logged to `.gpc/index.log`.
- `examples/hooks/graphify-neo4j-post-commit.sh` — refreshes the Graphify
  projection in Neo4j.

This keeps client requests fast and predictable while keeping write operations
explicit and inspectable.

## MCP Surface

The current server exposes sixteen read-only tools across five surfaces:

- **Identity / discovery** — `gpc.health`, `gpc.resolve_project`,
  `gpc.resolve_repo`, `gpc.list_projects`, `gpc.list_repos`,
  `gpc.index_status`.
- **Semantic retrieval** — `gpc.search`, `gpc.context`,
  `gpc.estimate_token_savings`. These run over Qdrant embeddings and accept
  an optional `repo` filter.
- **Structural queries** — `gpc.graph_neighbors`, `gpc.graph_summary`,
  `gpc.graph_path`, `gpc.graph_community`. These read the Neo4j Graphify
  projection and expose `confidence` on every edge; by default only
  `EXTRACTED` edges are returned. `graph_summary` splits god nodes into
  `god_nodes` (distinctive, likely architectural hubs) and `utility_hubs`
  (generic bootstrap/dispatch names like `run()` / `main()` / `connect()`)
  so the caller can ignore the second bucket without losing the signal.
- **Longitudinal self-metrics** — `gpc.self_metrics`, `gpc.graph_diff`.
  Writes and reads rows of ``gpc_self_metrics`` (populated at the end of
  every ``gpc-index`` and ``gpc graph-bridge`` run). Drives the drift
  detector of Fase 3 — comparing two snapshots surfaces size deltas,
  god-node churn, and movement in the confidence distribution.
- **Observability** — `gpc.mcp_usage`. Surfaces the server's own call log
  (every tool call writes one row to `gpc_mcp_calls`) so operators can
  confirm that AI clients are actually hitting the server.

Tool inputs, semantics and client setup are documented in
[mcp-clients.md](mcp-clients.md).

## See Also

- [Operations](operations.md) — install, validate, reset, index.
- [Automation](automation.md) — CLI shims and Git hooks.
- [Graphify and Neo4j](graphify.md) — graph consolidation across repos.
- [Token economy](token-economy.md) — what GPC saves and how it is measured.
