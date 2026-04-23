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

The current server exposes seven read-only tools (`gpc.health`,
`gpc.resolve_project`, `gpc.list_projects`, `gpc.index_status`, `gpc.search`,
`gpc.context`, `gpc.estimate_token_savings`).

Tool inputs, semantics and client setup are documented in
[mcp-clients.md](mcp-clients.md).

## See Also

- [Operations](operations.md) — install, validate, reset, index.
- [Automation](automation.md) — CLI shims and Git hooks.
- [Graphify and Neo4j](graphify.md) — graph consolidation across repos.
- [Token economy](token-economy.md) — what GPC saves and how it is measured.
