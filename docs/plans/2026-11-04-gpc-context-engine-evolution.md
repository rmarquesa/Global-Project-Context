# GPC context-engine evolution

## Direction

Improve GPC as a local-first context and engineering-memory service for existing
AI clients, rather than building another coding agent. The Augment Context
Engine and Code Review Memory are product references, not dependencies or claims
of equivalent retrieval quality.

## Delivery order

### 0. Correctness and safety (implemented; live validation pending)

- Scope file lookup, deletion, reset and freshness diagnostics by repository.
- Make per-file vector projection repairable after partial failure or vector loss.
- Enforce context budgets, retain unambiguous repo-qualified citations and pass
  graph annotations as structured data instead of reparsing rendered text.
- Detect populated but inconsistent indexes rather than reporting false health.
- Bind Docker-published services to loopback by default, scan the complete
  accepted file for recognized secrets and respect symlink policy in Git discovery.
- Add isolated regression tests and restore the curated type-checking gate.

Acceptance uses synthetic repositories and mocked services. Live destructive
smoke tests must only run against an explicitly disposable environment. A passing
unit suite does not certify the user's running Postgres/Qdrant/Neo4j installation.

Integrated validation on 2026-11-04:

- 246 unit/regression tests passed (149 before this increment).
- Ruff critical checks across source/tests and full lint on changed Python passed.
- Black passed for all 13 changed/new Python files.
- Mypy passed for the five curated modules; 94 Python files compiled successfully.
- Compose configuration validated with both HTTP and observability profiles.
- No real databases or running services were modified or restarted.

Remaining validation: run the live smoke tests against disposable services,
including the existing reset/rename/delete/repo-registry/cross-repo modules not
currently invoked by the CI smoke wrapper. Per-file repair is retry-safe, not a
distributed transaction. Phase 1 adds cooperative per-checkout serialization;
atomic snapshots of a concurrently edited source tree remain out of scope.

### 1. Fresh context (implemented; live latency validation pending)

Add an optional foreground polling watcher, debounced work queue and explicit
branch/worktree identity, plus shared cross-process index serialization. Test
create/update/delete/rename, branch switches and restart recovery. Avoid redundant
embedding of unchanged data. See [Automatic local indexing](../watcher.md).

Worktrees use separate registered repo identities; this is current-checkout
reconciliation, not a historical branch-index service. End-to-end edit-to-search
latency still needs measurement against live disposable services.

Integrated validation on 2026-11-04: 373 tests passed (246 before this increment),
including temporary Git repositories/worktrees, deterministic debounce/retry
tests, real subprocess locks and CLI-to-watcher-to-indexer dispatch with a mocked
backend. Ruff, Black, syntax compilation and the curated Mypy gate passed. The
independent security review's nonregular-state-file finding was fixed and covered
by FIFO, socket, directory and hardlink regressions. No watcher was enabled against
the user's repositories/services. The Windows locking branch remains untested on
this macOS host; end-to-end freshness with real stores is not yet benchmarked.

### 2. Task-aware retrieval (implemented; live evaluation pending)

Evaluate lexical and symbol retrieval alongside vectors, bounded graph expansion
and optional local reranking. Use the same representative tasks for baseline and
candidate runs; report retrieval quality, returned tokens and latency. Do not
equate fewer returned tokens with a correct answer.

The current implementation adds opt-in lexical/vector rank fusion, textual
identifier/path matching, deterministic task preferences and comparison metrics.
Semantic retrieval remains the default. See [Hybrid retrieval](../hybrid-search.md).
Learned reranking, an AST symbol index and graph-expanded candidate retrieval
remain future work; current graph notes are post-retrieval annotations.

Comparison reports retain semantic baselines, repository-qualified identities,
legacy path-only labels, MRR/nDCG, recall and measured latency. They do not yet
measure answer correctness or a task-level token/quality trade-off. SQL ownership
and parameterization have an independent review, but PostgreSQL execution/query
plans and representative production relevance remain unvalidated. No migration
or indexing operation was run against the user's stores.

Integrated validation on 2026-11-04: **494 tests passed** (373 before this
increment). Ruff passed repository critical checks and all 25 changed/new Python
files; Black passed those 25 files, syntax compilation passed 102 files, and the
curated five-module Mypy gate passed. CLI search/evaluation help and documentation
link targets were checked. The first evaluation implementation's legacy-path
matching regression was corrected before acceptance, with 32 evaluation tests;
the CLI/MCP/context integration added 34 tests. SQL remains contract-tested with
fake backends, not validated by execution on PostgreSQL.

### 3. Evidence-backed memory (implemented; live-store validation pending)

Build on the decisions schema with capture, approval, retrieval and invalidation.
Scope each memory to a project/repository/path and retain its source, confidence,
status and revision. Human-approved writes remain separate from MCP retrieval.
Surface contradictory or obsolete guidance instead of silently treating it as fact.

The current scope is explicit proposals backed by an indexed source hash,
approval, invalidation, same-key supersession and a separate read-only retrieval
surface. Source revision labels are caller-supplied, not verified Git revisions.
Freshness is relative to the index; semantic contradiction detection and automatic
chat-memory ingestion remain out of scope. See [Decision memory](../decision-memory.md).

Every feature delivery updates its documentation and the static product site.
The site distinguishes implemented capabilities, pending live validation and
future work; editing it does not publish a deployment.

Integrated validation on 2026-11-04: **682 tests passed** (494 before this
increment), including 82 backend lifecycle tests, 104 CLI/MCP interface tests and
two reset-contract regressions. Ruff and Black passed all 31 changed/new Python
files, syntax compilation passed 107 files, and the curated five-module Mypy gate
passed. Actual FastMCP dispatch tests cover strict numeric/boolean inputs and
read-only memory retrieval. An independent review found the new audit table was
missing from full-reset ordering; it is now explicitly dropped before decisions.
PostgreSQL reset/reapply semantics and concurrent transactions still require
disposable-service validation; local tests use SQLite adaptation/SQL contracts.

The static product site now covers watcher, hybrid retrieval and decision memory,
with implementation/validation/planned status separated. Desktop (1280px) and
mobile (390px) browser checks covered layout, keyboard interactions, four demo
presets and graph controls; no page overflow or new runtime errors were observed.
Site IDs, anchors and documentation targets were checked. Preview was stopped;
no deployment, migration or real-store operation was performed.

### 4. Engineering context (not implemented in this increment)

Add scoped connectors for ADRs, PRs and issues; relevant hierarchical instructions;
and diff-focused impact/review packs with related contracts, tests and decisions.
Keep source permissions and provenance attached to external content. Store runtime
verification evidence against the exact revision that was exercised.

## Non-goals for this delivery

No hosted multi-tenant service, billing, autonomous code editing, automatic PR
approval or production-service mutation. No inferred Augment retrieval internals
or unmeasured performance-parity claims.

## Validation correction discovered during OrbStack testing

The historical pytest totals above passed, but the default suite was not wholly
isolated: four pre-existing tests used live Neo4j, and one also appended synthetic
`consolidation-demo` graph-bridge metric rows to local Postgres. The live validation
guard detected two such rows; indexed content and decisions were unchanged.
Those tests now run only with explicit integration selection and in the CI
integration job; their unrelated telemetry observer is isolated. Do not interpret the historical totals as
proof that no local services were contacted.

Real PostgreSQL testing also found four older metrics/log tables missing from
full-reset ordering. The reset inventory now includes them, and a regression
checks every table declared by the migrations and child-before-parent ordering.
All destructive reset checks run only against an ownership-guarded temporary
database, never the existing `gpc` database.

After these corrections, the default suite passed **713 tests, with four
integration tests deselected**, while all service endpoints were overridden to
unavailable loopback ports. Ruff/Black also passed the entire original push range
(53 Python files), including previously unpushed commits, and Mypy/Compose checks
passed. The legacy `embedding` fixture header is accepted by both YAML loaders;
real PostgreSQL upgrade, reset/reapply, lexical SQL and decision lifecycle checks
passed 75 assertions in the ownership-guarded temporary database. A protected
backup was successfully restored before any real-schema promotion.

## Product references

- https://docs.augmentcode.com/context-services/mcp/overview.md
- https://docs.augmentcode.com/context-services/context-connectors/how-it-works.md
- https://docs.augmentcode.com/cosmos/experts-code-review-memory.md
- https://docs.augmentcode.com/cosmos/experts-memory.md
- https://docs.augmentcode.com/cli/rules.md
- https://docs.augmentcode.com/cosmos/experts-verifier.md
