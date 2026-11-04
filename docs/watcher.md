# Automatic local indexing

`gpc watch` is an optional foreground process that keeps an existing registered
Git working tree indexed while you edit. It does not start automatically, install
a daemon, rebuild Graphify, or change the MCP retrieval API.

## Start

Register the checkout first if necessary:

```bash
gpc init . --slug my-project --no-hooks
gpc watch .
```

For an already registered repository in a logical project:

```bash
gpc watch /path/to/api --project my-project --repo api
```

The supplied path must be the Git worktree root and match the registered repo's
canonical root. Watching a subdirectory or accidentally reusing another worktree's
repo identity is rejected rather than changing the registry.

## Scheduling

```bash
gpc watch . --interval 1 --debounce 2 --max-debounce 10 --retry-interval 5
```

- Polling discovers eligible paths and checks filesystem/Git identity changes.
  It uses the indexer's conservative discovery rules; it does not bypass secret,
  symlink, generated-file or size filters.
- A burst of edits is coalesced into one pending reconciliation. The maximum
  debounce bounds how long continuously arriving edits can postpone a run.
- Creation, modification, deletion and renaming trigger an incremental pass with
  pruning. The indexer hashes contents and avoids re-embedding healthy unchanged
  files.
- Only one run is active in the watcher. An edit or branch change observed during
  indexing schedules a follow-up rather than being acknowledged as already fresh.
- A failed or busy index run is retried without requiring another file edit.
- Every startup reconciles once, including after an interruption or offline edits.

This is polling, not an instantaneous filesystem notification service. Freshness
depends on the polling interval, debounce and indexing duration. Indexing still
requires the configured Postgres, Qdrant and Ollama services. Large repositories
may need a longer polling interval to reduce discovery overhead.

Errors in watcher state/events are categories, not raw exception bodies. Use
`gpc-status --project <slug>` and `gpc verify --project <slug>` to investigate
failed runs or unavailable services without exposing source text in watcher state.

## One pass and machine-readable events

```bash
# Reconcile once and return a success/failure exit code; do not stay running.
gpc watch . --once

# Stream one JSON object per event, suitable for local log collection.
gpc watch . --json
```

The final event includes `pending`, `indexed_runs`, `failed_runs` and `exit_code`.
If the checkout changes during `--once`, that attempt remains pending and returns
nonzero even if the indexer itself succeeded. Rerun after the checkout settles or
use continuous mode to let the watcher reconcile the follow-up automatically.

`Ctrl+C` or `SIGTERM` requests shutdown. A running synchronous index operation may
need to finish before the process exits. No source file is modified by the watcher.

## Branches and worktrees

The watcher identifies the canonical root, Git directory/common directory, branch
and HEAD commit. Branch/HEAD changes trigger reconciliation even when file
signatures are otherwise unchanged. Detached HEAD is an explicit state.

The index is the **current checkout**, not a historical index for every branch.
Branch switching replaces that repository's current indexed context. A Git
checkout and a multi-service index update are not an atomic snapshot; use the
watcher's pending/success status to understand whether reconciliation has finished.

Give linked worktrees separate registered repo slugs under the same logical
project. For example:

```bash
git worktree add ../my-project-feature -b feature
gpc init ../my-project-feature --project my-project --repo feature-worktree --no-hooks --no-index
gpc watch ../my-project-feature --project my-project --repo feature-worktree
```

Use `repo` filters when retrieving context from a particular worktree. The watcher
does not silently reuse the main checkout's registry entry or maintain a hidden
cross-worktree index.

## State and coordination

Local status is kept in `.gpc/watch-state.json`, with repository/revision identity
and recovery information, not source content. State is replaced atomically. A
corrupt or outdated state does not allow startup to skip reconciliation. `.gpc/`
is excluded from discovery, so writing state does not trigger an indexing loop.
If the repository was registered without `gpc init`, also add `.gpc/` to its Git
ignore rules before sharing or committing local operational metadata.

One watcher may own a checkout at a time. Index runs use a separate, nonblocking
repository lock shared by the watcher, manual indexing and Git-hook indexing.
Different worktree roots can index independently. Lock ownership is released on
exit; the lock files remain in place intentionally. Do not remove them to bypass
an active process. Symlinked metadata/lock paths are rejected.

Existing Git hooks can remain installed, but overlapping manual/hook runs may
report that the repository is busy; the watcher retries. To avoid duplicate
triggers for a particular Git command, use the existing hook control:

```bash
GPC_AUTO_INDEX=0 git commit
```

`--no-hooks` during init does not uninstall hooks that are already present.

## Scope

This increment does not add cloud synchronization, historical branch search,
Graphify regeneration, semantic/lexical reranking, or decision-memory ingestion.
It does not promise atomicity against concurrent source-tree mutation. See the
[evolution plan](plans/2026-11-04-gpc-context-engine-evolution.md) for later stages.
