# MCP Clients

GPC ships as a single local MCP server registered as `gpc` in every supported
client. There is one server instance per machine; project separation is
handled inside GPC by the registry and resolver.

This document is the canonical reference for the MCP tool surface, client
installation, transport options and troubleshooting.

## MCP Tools

The server exposes seventeen read-only tools.

| Tool | Purpose | Key inputs |
|---|---|---|
| `gpc.health` | Reports whether Postgres, Qdrant, Ollama and the embedding model are reachable. | — |
| `gpc.resolve_project` | Resolves a project by `cwd` or slug; returns identity, root and aliases. | `cwd`, `project` |
| `gpc.resolve_repo` | Resolves a `(project, repo)` tuple. Use this inside a multi-repo project so subsequent `search`/`context` calls can scope by repo. | `cwd`, `project`, `repo` |
| `gpc.list_projects` | Lists registered projects with their repos and aliases. | — |
| `gpc.list_repos` | Lists repositories under a project. | `project`, `cwd` |
| `gpc.index_status` | Returns indexed file count, chunk count, Qdrant point count and recent run summaries. | `cwd`, `project`, `runs` |
| `gpc.search` | Returns ranked chunk hits for a semantic query, hydrated from Postgres. Pass `repo` to filter. | `query`, `cwd`/`project`, `limit`, `content_chars`, `repo` |
| `gpc.context` | Returns one bounded context block ready to inject into a prompt. Pass `repo` to filter; set `include_graph=true` for hybrid retrieval (each chunk gets a footer with neighbours + confidence from Neo4j). | `query`, `cwd`/`project`, `max_chunks`, `max_chars`, `repo`, `include_graph`, `graph_min_confidence` |
| `gpc.estimate_token_savings` | Reports `indexed_tokens`, `retrieved_tokens` and the estimated saving for a query. | `query`, `cwd`/`project` |
| `gpc.graph_neighbors` | Neighbours of a node in the Neo4j projection, with typed relations and confidence. Answers "who uses X?". | `node`, `cwd`/`project`, `depth`, `min_confidence`, `relations`, `limit` |
| `gpc.graph_summary` | God nodes, repo breakdown, cross-repo bridges and communities. The structured equivalent of `GRAPH_REPORT.md`. | `cwd`/`project`, `top_k_gods`, `include_cohesion` |
| `gpc.graph_path` | Shortest path between two nodes in the Neo4j projection. Each hop carries relation + confidence. | `a`, `b`, `cwd`/`project`, `max_hops`, `min_confidence` |
| `gpc.graph_community` | Members, repos and external bridges of one community. Run after `graph_summary` to drill into a cluster. | `community_id`, `cwd`/`project`, `top_members`, `top_external_bridges` |
| `gpc.self_metrics` | List recent ``gpc_self_metrics`` snapshots for a project; optionally collect a fresh one first. | `cwd`/`project`, `limit`, `collect`, `source` |
| `gpc.graph_diff` | Diff two metric snapshots: numeric deltas, god-node churn, confidence-distribution shift. | `cwd`/`project`, `window_hours`, `from_id`, `to_id` |
| `gpc.drift_signals` | List stored drift signals; optionally detect and persist fresh rule-based signals from the latest metric snapshots. | `cwd`/`project`, `window_hours`, `detect`, `persist`, `limit` |
| `gpc.mcp_usage` | Aggregates the server's own call log. Use it to confirm AI clients are actually hitting GPC. | `window_hours`, `cwd`/`project` |

**Project resolution**. Every tool accepts either `project` (a registered slug
or alias) or `cwd` (the active repository path). Pass `project` when you
already know the slug; pass `cwd` when the client is operating inside a
repository. Without either, the server falls back to `GPC_CALLER_CWD` and
finally to its own working directory.

**Repo scoping**. A single project can own multiple repositories (e.g.
`alugafacil` owns `workers-gateway`, `workers-users`, `web`). Pass a single
slug (`repo: "workers-gateway"`) or a list (`repo: ["workers-gateway", "workers-users"]`)
to `gpc.search` and `gpc.context` to keep retrieval scoped. When the client
is already inside a repo directory, `cwd` resolution picks the correct repo
automatically — no explicit `repo` needed.

**Bounded retrieval**. `gpc.context` enforces `max_chunks` and `max_chars` so
the returned block fits inside an AI prompt. Use `gpc.search` when you want
the raw matches with relevance scores instead.

**Semantic vs. structural**. `gpc.search` / `gpc.context` answer "what looks
like this?" — they return text chunks ranked by embedding similarity. The
`gpc.graph_*` tools answer "what connects to this?" — they read the Neo4j
projection and return nodes and typed edges (calls, imports, cross-repo
bridges). Use them together: `graph_summary` to orient on the project,
`graph_neighbors` to discover callers and dependencies, `graph_path` to trace
a chain between two symbols, then `search` / `context` to get the actual code
behind any interesting node.

**Honesty on graph edges**. Every edge returned by a `graph_*` tool carries a
`confidence` (`EXTRACTED`, `INFERRED`, or `AMBIGUOUS`) and a numeric
`confidence_score`. `EXTRACTED` means the relationship is explicit in source
(calls, imports, citations). `INFERRED` is a heuristic — e.g. two symbols in
different repos share the same source file path. `AMBIGUOUS` is kept only
when the caller explicitly passes `min_confidence="AMBIGUOUS"`. The default
`min_confidence="EXTRACTED"` hides heuristic bridges so the caller cannot
treat them as fact by accident.

## Auditing MCP Usage

Every tool call logs one row to the `gpc_mcp_calls` table (see migration
`0006_mcp_call_log.sql`). Arguments are filtered to a known-safe subset
(slugs, counts, query strings — no raw chunk content) and every field in
the result is reduced to a small metadata blob.

Use `gpc.mcp_usage` to see the aggregate without opening a SQL client:

```json
{
  "window_hours": 24,
  "totals": { "total": 147, "ok": 145, "failed": 2, "distinct_clients": 2 },
  "by_tool":   [ { "tool": "gpc.search",  "calls": 63, "errors": 0, "avg_ms": 480 } ],
  "by_client": [ { "client": "claude",    "calls": 110 } ],
  "by_project":[ { "project": "alugafacil","calls": 120 } ]
}
```

For finer-grained audits, query the table directly:

```sql
select tool, count(*) as calls, count(*) filter (where not success) as errors
from gpc_mcp_calls
where called_at > now() - interval '1 day'
group by tool
order by calls desc;
```

**Client identification** is best-effort: some environments set
`CLAUDE_CODE_SESSION_ID` / `CODEX_CLI_VERSION` / `COPILOT_AGENT_VERSION`
when they spawn the MCP server, and the log picks those up automatically.
`gpc install-clients` labels supported clients explicitly with
`GPC_MCP_CLIENT=<client>`. If you wire a client manually, add the same
environment variable in that client's MCP config. Unknown clients are logged
as `unknown` rather than dropped.

**Retention** is available through the maintenance CLI. A cron-friendly dry
run looks like:

```bash
gpc maintenance retention --mcp-days 30 --token-days 90 --dry-run
```

**Failure mode**: if Postgres is unreachable when a tool call happens, the
logger logs a warning to stderr and the tool response goes through
unaffected. Never break the MCP surface because of an observability
hiccup.

## Install Client Configuration

Install the GPC MCP server into every supported client found on the machine:

```bash
gpc install-clients
```

Validate without modifying configuration:

```bash
gpc install-clients --validate-only
```

Install into specific clients only:

```bash
gpc install-clients --clients codex,claude-code,copilot
```

The installer creates timestamped backups of any client configuration file it
modifies.

### Supported clients

| Client | Configuration file |
|---|---|
| Codex | Managed by `codex mcp add gpc`. |
| Claude Code | Managed by `claude mcp add --scope user gpc`. |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| OpenClaw | Managed by `openclaw mcp set gpc`. |
| Gemini CLI / Code Assist | `~/.gemini/settings.json` |
| Gemini Antigravity | `~/.gemini/antigravity/mcp_config.json` |
| GitHub Copilot in VS Code | `~/Library/Application Support/Code/User/mcp.json` |

### Manual client configuration

If your client is not auto-supported, register a stdio MCP server with this
shape:

```json
{
  "mcpServers": {
    "gpc": {
      "command": "/usr/bin/env",
      "args": [
        "GPC_MCP_CLIENT=manual",
        "/absolute/path/to/GPC/venv/bin/python",
        "/absolute/path/to/GPC/gpc_mcp_server.py"
      ],
      "cwd": "/absolute/path/to/GPC"
    }
  }
}
```

A working template lives at `mcp_config.example.json`.

## Transport Modes

### Stdio (default)

Each client launches `gpc_mcp_server.py` as a subprocess on demand. No
long-running daemon is required, and no port is exposed. This is the broadest
compatibility path for desktop clients.

### HTTP

For clients that prefer a URL, run:

```bash
gpc-mcp-http --host 127.0.0.1 --port 8765
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

Or via Docker Compose:

```bash
docker compose --profile http up -d gpc-mcp-http
```

> ⚠️ **Do not bind `gpc-mcp-http` to a public interface** without putting an
> authenticating reverse proxy in front. The MCP server is read-only but still
> returns indexed chunks of your code. See [SECURITY.md](../SECURITY.md).

## Usage Examples

Tell the client to call the GPC MCP server:

```text
Use the gpc MCP server. Call gpc.context with project=gpc and
query="how does the indexer detect secret-like content?".
```

```text
Use gpc.search with cwd=/path/to/repo and query="who calls the auth middleware?".
Return the top 5 chunks.
```

```text
Use gpc.estimate_token_savings with project=gpc and the same query as above
to show how much context would otherwise have been sent.
```

## Troubleshooting

### The client does not see the `gpc` tools

1. Confirm the server is registered: `gpc install-clients --validate-only`.
2. Verify the absolute paths in the client config still match this checkout —
   if you moved the GPC directory or recreated the virtualenv, rerun
   `gpc install-clients`.
3. Restart the client. Most MCP clients only load configuration at startup.

### Client shows the server as `failed` / `Server disconnected`

The server crashed before or during the MCP handshake. Check
`~/Library/Logs/Claude/mcp-server-gpc.log` (macOS Claude Desktop) for the
actual exception.

The most common cause on a fresh install is **`psycopg` cannot find
`libpq`**. That happens because GUI MCP clients spawn the server with a
trimmed `PATH` (for example `/bin:/usr/sbin:/sbin`) so Homebrew-installed
`libpq` is invisible even though it works in your terminal. Fix it by
installing the `psycopg` wheel that bundles `libpq`:

```bash
./venv/bin/pip install 'psycopg[binary]'
```

`requirements.txt` already pins `psycopg[binary]`, so a clean
`./venv/bin/pip install -r requirements.txt` gets this right out of the
box. After installing, restart the client so it respawns the server.

### `gpc.context` returns empty results

1. Run `gpc-status --project <slug>` to confirm the project has indexed
   chunks. A fresh project that has never been indexed returns nothing.
2. Run `gpc-search "<your query>" --project <slug>` from the CLI to compare
   against the MCP path. If the CLI also returns nothing, the issue is index
   coverage or query phrasing rather than MCP transport.
3. Confirm Ollama is reachable: `curl http://localhost:11434/api/tags`.

### `gpc.health` reports a failing service

| Failure | Likely cause | Fix |
|---|---|---|
| Postgres unreachable | Container not running. | `docker compose up -d postgres` |
| Qdrant unreachable | Container not running. | `docker compose up -d qdrant` |
| Ollama unreachable | Service not started, or wrong host. | Start Ollama; check `GPC_OLLAMA_HOST`. |
| Embedding model missing | Model not pulled. | `ollama pull nomic-embed-text` |

### Where to find logs

| Source | Path |
|---|---|
| Auto-index hooks | `<project>/.gpc/index.log` |
| Graphify hook | `<project>/.gpc/graphify-neo4j.log` |
| Docker services | `docker compose logs <service>` |
| MCP server (stdio) | Captured by the launching client. |
| MCP server (HTTP) | Stdout of `gpc-mcp-http`. |

## See Also

- [Architecture](architecture.md) — what the MCP server is talking to.
- [Operations](operations.md) — install, validate, reset, index.
- [Token economy](token-economy.md) — interpreting `gpc.estimate_token_savings`.
