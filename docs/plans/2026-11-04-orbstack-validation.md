# OrbStack validation — 2026-11-04

The operator authorized local restarts and live testing while preserving existing
data. Validation used a protected PostgreSQL backup, an ownership-guarded temporary
database, and then the existing local GPC installation. No real-project reset or
index pruning was performed.

## Backup and isolated SQL validation

- Custom-format backups were stored outside the repository in a private artifact
  directory (0700), with dump files at 0600. The initial backup was successfully
  restored and all 18 original table counts and migration checksums verified.
- **75 PostgreSQL checks passed**: upgrade from 0009 to 0011, legacy decisions,
  lexical SQL/literal escaping, project/repo isolation, approval/supersession,
  transactional audit rollback, stale/recreated sources, full schema reset and
  fresh migrations. The fresh schema had 19 tables and 28 validated foreign keys.
- Live reset testing found four older metrics/log tables absent from the reset
  inventory. The corrected helper now removes them before their parents. A unit
  regression checks coverage of every table declared in the migrations.
- The initial dump SHA256 is
  `4b5dfe90bc691bc3606e6996c394c5713e0132142fe3e2c2dc917054f494eee6`.
  A fresh pre-promotion dump SHA256 is
  `75a685593d14774b238d3d4bf0f05852c81024a9cc4fe6250647b308ef67ef88`.
  Backups are retained privately, not committed or published.

## Local promotion and MCP

**32 rollout/protocol checks passed.** Migrations 0010/0011 were applied to the
existing local database after restore validation. PostgreSQL, Qdrant and Neo4j
were recreated with unchanged images and named volumes, replacing all-interface
published ports with loopback bindings. The HTTP image was built from current
source; four core file hashes matched the checkout.

The HTTP endpoint is `http://127.0.0.1:8765/mcp`. Actual MCP initialization listed
18 tools. Health, decision retrieval, lexical/semantic/hybrid search, graph
summary and bounded context were exercised. One context used 1,992 of 2,000
characters and retained its citation. The existing Ollama model produced 768
finite dimensions; no model download was needed.

Preserved application data:

| Store | Before and after rollout |
| --- | --- |
| Postgres | 1 project, 1 repo, 154 files, 477 chunks, 0 decisions |
| Qdrant | 478 points |
| Neo4j | 3,027 nodes, 11,053 relationships |

Protocol tests intentionally added seven MCP call records and four token-saving
samples. The existing one-point vector/chunk discrepancy was reported, not erased.

## Corrections to test isolation and CI

The first push also included older local commits. CI exposed formatting defects
in that larger range and rejection of the existing evaluation fixture's top-level
`embedding` metadata. Both loaders now accept that legacy annotation without
overriding measured runtime embedding metadata.

Four pre-existing tests under `tests/test_*.py` actually required Neo4j. One also
appended two synthetic `consolidation-demo` metric rows to local Postgres during
testing. The guard detected and recorded that telemetry drift (32 to 34); indexed
content and decisions were unchanged. These tests now have explicit integration
selection, UUID fixture scopes and a stub for the unrelated metrics observer.
CI still runs them, but only in its disposable-service job.

The corrected default suite passed **713 tests, four integration tests deselected**
with every backing-service endpoint overridden to an unavailable loopback port.
Ruff/Black passed the full 53-file Python push range; Mypy and Compose checks passed.

## Isolated indexing and watcher validation

**19 further checks passed** against the owned temporary PostgreSQL database and
a separate, ownership-labeled Qdrant container using tmpfs storage. The existing
Ollama model embedded only synthetic fixture code. Eleven real index runs had
zero failed files; unchanged reindexing made zero file-embedding calls.

Checks covered all three retrieval modes, missing-vector repair, lock contention
and retry, and continuous watcher reconciliation after edits, creates, renames,
deletes, branch and HEAD changes. Seven watcher reconciliations completed without
failure, with clean shutdown and released locks. Changing indexed evidence hid
the previously approved decision from default retrieval. One synthetic local
edit became searchable in **1.142 seconds**; this is an observation, not a benchmark.

The owned database and isolated Qdrant container were removed and their absence
verified. Backups, harnesses and reports remain protected outside the repository.
Production business counts, graph, vectors and named volumes were unchanged;
this phase produced no production telemetry delta. All four GPC containers were
still running after cleanup. No actual repository was reindexed.

## Interpretation and remaining limits

- A four-query evaluation against the existing index executed successfully, but
  semantic recall@5 was **0.10** and MRR@5 **0.0625**. This is not a relevance gain
  or performance-parity claim.
- The registered repository still points to the prior worktree. Its identity and
  index were not silently redirected to the current checkout.
- Successful local lifecycle checks do not establish production concurrency,
  large-corpus performance, representative relevance or answer correctness.
- Credentials with GitHub `workflow` permission are required to push the CI
  workflow correction; local validation is not a substitute for the remote CI run.
