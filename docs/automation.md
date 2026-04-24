# Automation

GPC automates two layers:

1. **Global commands** installed into your `PATH` by `./install.sh`.
2. **Per-project Git hooks** installed by `gpc init` to keep each project's
   index in sync.

A third optional layer pushes per-repository [Graphify](graphify.md) graphs
into Neo4j after each commit. That layer is independent and documented in
[graphify.md](graphify.md).

The MCP server itself is read-only. Indexing and embedding writes always
happen through the CLI or the Git hooks below, so expensive operations stay
explicit, inspectable and logged.

## Global Commands

`./install.sh` installs the following shims into `~/.local/bin` (override with
`--bin-dir`):

| Command | Purpose |
|---|---|
| `gpc` | Main CLI: project init, doctor, migrate, install-clients, token-savings. |
| `gpc-index` | Index a project root. |
| `gpc-status` | Report indexed file/chunk/Qdrant counts and recent runs. |
| `gpc-search` | Semantic search across an indexed project. |
| `gpc-mcp-http` | Run the MCP server over local HTTP. |

The shims point back to this GPC checkout and inject `PYTHONPATH`, so they
work from any project directory.

## Per-Project Git Hooks

Inside any Git repository:

```bash
gpc init . --slug project-slug --name "Project Name"
```

For a repository that belongs to a logical multi-repo project, initialize with
the parent project and repo slug instead:

```bash
gpc init . --project project-slug --repo repo-slug --name "Repo Name"
```

The generated hooks read `.gpc.yaml` and keep using the same project/repo pair
for automatic indexing.

This writes:

- `.gpc.yaml` — project identity and the GPC root path.
- `.git/hooks/post-commit`
- `.git/hooks/post-merge`
- `.git/hooks/post-checkout`
- A `.gpc/` entry in the project's `.gitignore`.

The hooks run incremental indexing in the background:

```bash
gpc-index --no-prune .
```

Logs go to `.gpc/index.log`.

### Common scenarios

Disable the auto-index hook for a single command:

```bash
GPC_AUTO_INDEX=0 git commit
```

Skip hook installation during init:

```bash
gpc init . --no-hooks
```

Skip the initial full index (only register the project):

```bash
gpc init . --no-index
```

Replace existing GPC-managed hooks (e.g. after a path change):

```bash
gpc init . --force
```

`--force` only replaces hooks that GPC originally installed. Existing
non-GPC hooks are preserved.

### What the hook indexes

The auto-index call uses the same conservative defaults as `gpc-index`:

- Git discovery via `git ls-files --cached --others --exclude-standard`.
- Skips generated folders, binaries, oversized files, symlinks (unless
  `--follow-symlinks`), unsupported extensions and obvious secret-like
  content.
- Uses content hashes — unchanged files are not re-embedded.
- Does **not** prune deleted files (the hook uses `--no-prune` to stay
  cheap). Run `gpc-index .` manually for a full pass that prunes.

### Manual indexing

When you want immediate control or a full pruning pass:

```bash
gpc-index /path/to/project --slug project-slug --name "Project Name"
```

See [operations.md](operations.md#manual-indexing) for the full flag table.

## HTTP MCP

The default transport is stdio because it requires no daemon and is
universally compatible. Run the MCP server over HTTP when a client requires a
URL or when you want to share one server across local processes:

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

> ⚠️ Bind to `127.0.0.1` only unless you put authentication in front. See
> [SECURITY.md](../SECURITY.md#mcp-http-exposure).

## See Also

- [Operations](operations.md) — install, validate, manual indexing.
- [MCP clients](mcp-clients.md) — register the server in AI clients and
  troubleshoot.
- [Graphify and Neo4j](graphify.md) — optional per-repository graph
  consolidation hook.
