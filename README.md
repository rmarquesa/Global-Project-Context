# GPC - Global Project Context

GPC is a local persistent context layer for AI coding tools.

GitHub: [rmarquesa/Global-Project-Context](https://github.com/rmarquesa/Global-Project-Context)

Author: Rodrigo Alves · [LinkedIn rodrigomarquesalves](https://www.linkedin.com/in/rodrigomarquesalves)

It indexes local projects, stores durable metadata and searchable chunks, and
exposes bounded context through a single MCP server. The goal is to help tools
such as Codex, Claude Code, OpenClaw, Gemini, and GitHub Copilot recover project
knowledge across sessions without pasting whole repositories into prompts.

## Why

AI coding tools often start each session without enough project memory. GPC
keeps that memory outside the chat:

- project files and docs are indexed intentionally;
- embeddings are generated locally with Ollama;
- chunks are retrieved semantically from Qdrant;
- structured state is stored in Postgres;
- graph relationships can be projected into Neo4j;
- MCP clients ask for compact context when they need it.

## Status

Early-stage, local-first, read-only MCP surface.

Indexing can be run manually with `gpc-index` or installed as Git hooks in each
project with `gpc init`. The MCP server retrieves context but does not mutate
indexes directly.

## Main Entrypoints

For AI tools, the default entrypoint is `gpc_mcp_server.py`. Codex, Claude,
OpenClaw, Gemini, and Copilot can start it as an MCP stdio server.

For humans, the public commands are:

- `gpc`: main CLI.
- `gpc-index`: index the current or given project.
- `gpc-status`: inspect indexed state.
- `gpc-search`: search project context.
- `gpc-mcp-http`: run MCP over local HTTP.

The reusable implementation lives under the `gpc/` Python package. Internal
admin modules live under `scripts/`. Smoke tests live under `tests/smoke/`.

## Documentation

- [Architecture](docs/architecture.md) — components and how they fit together.
- [Operations](docs/operations.md) — install, validate, reset, index.
- [MCP clients](docs/mcp-clients.md) — server tools, client setup, troubleshooting.
- [Automation](docs/automation.md) — global CLI shims and per-project Git hooks.
- [Graphify and Neo4j](docs/graphify.md) — relationship-graph consolidation.
- [Token economy](docs/token-economy.md) — what GPC saves and how it is measured.
- [Quickstart with 3 projects](docs/quickstart-3-projects.md) — multi-repo example.
- [Scripts](docs/scripts.md) — package and command inventory.

Contributor and security notes live in [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). Landing page source lives in [site/](site/).

## Stack

- Postgres with pgvector image for durable structured state.
- Qdrant for semantic vector memory.
- Ollama for local embeddings.
- Neo4j for rebuildable graph projections.
- Graphify for per-repository knowledge graphs consolidated by project in Neo4j.
- MCP stdio and HTTP server modes for AI client integration.

## How It Works Technically

GPC separates indexing, storage, retrieval, and AI-client access.

```text
project folder
  |
  | gpc init / gpc-index / Git hooks
  v
GPC indexer
  |
  +-- reads allowed files and skips ignored or secret-like content
  +-- chunks file content into bounded text blocks
  +-- stores projects, files, chunks, hashes, runs, entities and relations in Postgres
  +-- asks Ollama for local embeddings for each chunk
  +-- stores vectors in Qdrant with payload IDs that point back to Postgres
  +-- can project relationship data into Neo4j for graph traversal

AI client
  Codex / Claude / OpenClaw / Gemini / Copilot
  |
  | MCP stdio or local HTTP
  v
GPC MCP server
  |
  +-- embeds the user's query with Ollama
  +-- searches the relevant project vectors in Qdrant
  +-- hydrates the matched chunks from Postgres
  +-- returns a compact context block to the AI tool
```

Postgres is the source of truth. It owns project registration, aliases,
indexed files, chunk metadata, hashes, index runs, durable entities, durable
relations and graph projection history.

Qdrant is the semantic search layer. It stores vectors for chunks and returns
the closest matches for a query, but it does not own canonical project state.

Ollama is the embedding provider. By default GPC uses
`nomic-embed-text:latest`, currently configured with 768-dimensional vectors.
Embeddings are generated locally, so project text does not need to be sent to a
remote embedding API.

Neo4j is a rebuildable graph read model. It is useful for questions such as
"which worker calls which service?" or "which modules touch the same database
tables?". It complements Postgres; it does not replace Postgres as the durable
source of truth. In the default Neo4j Community setup, GPC keeps all graph data
in the default database and separates it by `project_slug` and `repo_slug`.
This lets one logical project consolidate Graphify graphs from multiple Git
repositories.

The MCP server is intentionally read-only. AI tools can ask for health,
project resolution, status, search results, compact context, and token-savings
estimates. Index writes happen through `gpc-index`, `gpc init`, or project Git
hooks.

## Token Savings

GPC saves prompt tokens by avoiding the repeated "paste the whole project into
the chat" pattern. The AI client receives only the chunks that match the task
instead of the entire indexed corpus.

The stack is what makes this measurable:

- Postgres stores the indexed chunks and their token counts.
- Qdrant retrieves only the most relevant chunks for a query.
- MCP enforces bounded context through `max_chunks` and `max_chars`.
- Ollama generates local embeddings without spending remote LLM context.

You can measure the reduction for any indexed project:

```bash
gpc token-savings \
  "how does GPC work technically and how does it reduce prompt tokens with Postgres Qdrant Neo4j Ollama Graphify and MCP?" \
  --project gpc
```

Measured example on this repository after indexing this documentation update:

```text
project=gpc
files=60
chunks=120
indexed_tokens=25737
retrieved_tokens=1387
saved_tokens=24350
savings=94.61%
```

The calculation is:

```text
savings_percent = (indexed_tokens - retrieved_tokens) / indexed_tokens
```

This proves the local retrieval layer reduced the context sent for that query
from 25,737 indexed tokens to 1,387 retrieved tokens. It is not a universal
guarantee: savings depend on project size, indexed coverage, the question, and
the configured retrieval limits. It also does not reduce model output tokens or
the reasoning work performed after the context is retrieved.

## Quickstart

### 1. Requirements

Install these first:

- Python 3.12 or newer.
- Docker and Docker Compose.
- Ollama running on the host machine.
- Git.

GPC uses Ollama for local embeddings. The default model is:

```text
nomic-embed-text:latest
```

### 2. Configure Environment

Start from the example file:

```bash
cp .env.example .env
```

Default local service configuration:

```env
GPC_POSTGRES_USER=gpc
GPC_POSTGRES_PASSWORD=gpcpass
GPC_POSTGRES_DB=gpc
GPC_POSTGRES_PORT=5433

GPC_QDRANT_HOST=localhost
GPC_QDRANT_HTTP_PORT=6333
GPC_QDRANT_GRPC_PORT=6334

GPC_OLLAMA_HOST=http://localhost:11434
GPC_OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest
GPC_VECTOR_SIZE=0

GPC_POSTGRES_DSN=postgresql://gpc:gpcpass@localhost:5433/gpc

GPC_NEO4J_URI=bolt://localhost:7687
GPC_NEO4J_USER=neo4j
GPC_NEO4J_PASSWORD=gpcneo4jpass
GPC_NEO4J_BROWSER_PORT=7474
GPC_NEO4J_BOLT_PORT=7687

GPC_MCP_HTTP_HOST=127.0.0.1
GPC_MCP_HTTP_PORT=8765
GPC_MCP_HTTP_PATH=/mcp
```

For shared machines, change the default passwords before starting services.

### 3. Install GPC

```bash
./install.sh
```

The installer creates the virtual environment, installs dependencies, starts
Postgres, Qdrant and Neo4j, pulls the Ollama embedding model, runs migrations,
initializes Qdrant and installs command shims.

Useful installer options:

```bash
./install.sh --skip-clients
./install.sh --clients codex,claude-code,copilot
./install.sh --bin-dir ~/.local/bin
```

Validate the installation:

```bash
gpc doctor
gpc install-clients --validate-only
```

### 4. Configure AI Clients

Install GPC as MCP server `gpc` in supported clients:

```bash
gpc install-clients
```

Supported clients:

- Codex
- Claude Code
- Claude Desktop
- OpenClaw
- Gemini CLI / Gemini Code Assist
- Gemini Antigravity
- GitHub Copilot in VS Code

The stdio MCP config uses this shape:

```json
{
  "mcpServers": {
    "gpc": {
      "command": "/absolute/path/to/GPC/venv/bin/python",
      "args": ["/absolute/path/to/GPC/gpc_mcp_server.py"],
      "cwd": "/absolute/path/to/GPC"
    }
  }
}
```

For tools that prefer HTTP, run:

```bash
gpc-mcp-http --host 127.0.0.1 --port 8765
```

HTTP MCP endpoint:

```text
http://127.0.0.1:8765/mcp
```

Docker Compose HTTP mode:

```bash
docker compose --profile http up -d gpc-mcp-http
```

### 5. Index A Repository

Inside a Git repository:

```bash
cd /path/to/project
gpc init . --slug project-slug --name "Project Name"
```

This creates `.gpc.yaml`, adds `.gpc/` to `.gitignore`, installs
`post-commit`, `post-merge` and `post-checkout` hooks, and runs the first
index.

Manual indexing:

```bash
gpc-index . --slug project-slug --name "Project Name"
```

Search:

```bash
gpc-search "how does authentication work?" --project project-slug
```

Status:

```bash
gpc-status --project project-slug
```

Token savings:

```bash
gpc token-savings "how does authentication work?" --project project-slug
```

### 6. Optional: Consolidate Repository Graphs With Graphify

GPC can consolidate per-repository [Graphify](https://github.com/rmarquesa/Graphify)
graphs into the bundled Neo4j Community database, separated by `project_slug`
and `repo_slug`. This adds graph-traversal questions on top of semantic
retrieval (e.g. *which worker calls which service*).

Setup steps, environment variables and Cypher examples live in
[docs/graphify.md](docs/graphify.md).

## MCP Tools

The MCP server exposes seven read-only tools:

- `gpc.health` — service availability.
- `gpc.resolve_project` — look up a project by `cwd` or slug.
- `gpc.list_projects` — list registered projects.
- `gpc.index_status` — files, chunks, Qdrant points and recent runs.
- `gpc.search` — ranked semantic chunks for a query.
- `gpc.context` — bounded context block ready for a prompt.
- `gpc.estimate_token_savings` — measured saving for a query.

Pass `project` when you know the slug, or `cwd` when the client is operating
inside a repository. Full descriptions and client setup live in
[docs/mcp-clients.md](docs/mcp-clients.md).

## Landing Page

Open the static page locally:

```bash
open site/index.html
```

The landing page is static and can be published with any static hosting setup.

## Security

GPC is designed to index local code and notes. Review [SECURITY.md](SECURITY.md)
before using it with private repositories or shared machines.

The indexer skips common secret files and obvious secret-like content by
default, but no automated scanner is a substitute for reviewing what you index.

## License

Code, documentation and website content are licensed under the GNU Affero
General Public License v3.0 or later. See [LICENSE](LICENSE).

The goal is strong copyleft: if someone distributes a modified version or runs a
modified network service based on GPC, the corresponding source should remain
available under the same license.

AGPL keeps the code open source, but it does not prohibit commercial use and it
does not protect the abstract idea behind the project. See [NOTICE.md](NOTICE.md)
for the project intent and legal boundaries.
