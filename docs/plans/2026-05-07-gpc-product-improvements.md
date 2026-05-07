# GPC Product Improvements Implementation Plan

> **For Hermes:** Use subagent-driven-development or Claude Code `@coder` to implement this plan PR-by-PR. Keep MCP read-only. Implement writes only through CLI/indexer/hooks/scripts. Use TDD for production behavior.

**Goal:** Implement the next GPC improvements/features as a staged, testable roadmap without increasing the blast radius of the MCP surface.

**Architecture:** Keep Postgres as the source of truth; keep Qdrant and Neo4j as rebuildable read-models. Add new capabilities first as CLI/offline modules with bounded MCP read-only exposure only when the underlying data is already persisted and safe to query. Build small, reviewable PRs with local smoke validation.

**Tech Stack:** Python, psycopg/Postgres, Qdrant, Ollama embeddings, Neo4j/Graphify, MCP, shell smoke wrappers, GitHub Actions.

---

## Execution rules

Cross-reference: keep this plan aligned with `docs/roadmap.md`; if scope/order diverges, update both documents in the same PR.

1. Work in small PRs in the order below.
2. For every behavior change: write failing test first, run it, implement minimal code, run it green.
3. Do not add write-capable MCP tools.
4. Do not run destructive commands (`reset`, project delete, graph reset with `--yes`) without explicit user confirmation.
5. Do not commit or push unless Rodrigo explicitly asks.
6. After modifying code, run:

```bash
./venv/bin/python -m compileall -q gpc scripts tests
./venv/bin/python -m ruff check --select E9,F63,F7,F82 gpc scripts tests
./scripts/run_smoke_tests.sh
```

7. For changed Python files, also run:

```bash
changed_py=$(git diff --name-only -- '*.py')
if [ -n "$changed_py" ]; then
  printf '%s\n' "$changed_py" | xargs ./venv/bin/python -m ruff check
  printf '%s\n' "$changed_py" | xargs ./venv/bin/python -m black --check
fi
```

8. After each PR, use `@reviewer`; after review pass, use `@tester`.
9. After code changes, refresh project context:

```bash
gpc-index . --slug gpc --name GPC --description 'Global Project Context infrastructure'
graphify update .
```

---

## PR 1 — Graph self-visibility and Graphify/GPC alignment

**Objective:** Make the GPC project visible to its own graph MCP tools and prevent regressions where `gpc.graph_summary(project="gpc")` returns `found=false` after Graphify update.

**Why first:** Semantic retrieval is already dogfooding the repo. Graph retrieval is the current gap.

### Task 1.1: Reproduce the current graph visibility failure

**Files:**
- Test: `tests/smoke/graph_self_visibility_smoke_test.py`

**Steps:**
1. Add a smoke test that calls `graph_summary(project_slug="gpc")` or the equivalent internal function.
2. Assert that the result is found and contains non-empty graph counts/god nodes.
3. Run the test and confirm it fails if the current MCP graph state still reports no graph.

**Command:**

```bash
./venv/bin/python -m tests.smoke.graph_self_visibility_smoke_test
```

**Expected RED:** failure shows no Graphify projection for `gpc` or empty graph summary.

### Task 1.2: Diagnose slug/projection mismatch

**Files:**
- Inspect: `gpc/graph.py` (`project_graph_to_neo4j` and projection persistence)
- Inspect: `gpc/graph_query.py`
- Inspect: `gpc/cross_repo.py`
- Inspect: `gpc/self_metrics.py`

**Steps:**
1. Trace how Graphify project slug is written to Neo4j.
2. Trace how `graph_summary(project="gpc")` filters Neo4j nodes.
3. Identify whether mismatch is `project_slug`, `GraphifyProject.slug`, repo slug, or missing projection run.
4. Document the finding in the test or code comment only if it clarifies a non-obvious invariant.

### Task 1.3: Implement minimal projection/lookup fix

**Files:**
- Modify: `gpc/graph_query.py` and/or projection code, depending on root cause
- Test: `tests/smoke/graph_self_visibility_smoke_test.py`

**Steps:**
1. Implement the minimal correction.
2. Run the new smoke test.
3. Run existing graph smoke tests.

**Commands:**

```bash
./venv/bin/python -m tests.smoke.graph_self_visibility_smoke_test
./venv/bin/python -m tests.smoke.graph_query_smoke_test
./venv/bin/python -m tests.smoke.graph_quality_smoke_test
./venv/bin/python -m tests.smoke.self_metrics_smoke_test
```

### Task 1.4: Add graph self-visibility to the smoke wrapper

**Files:**
- Modify: `scripts/run_smoke_tests.sh`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/operations.md`

**Steps:**
1. Add the new smoke module to the wrapper after graph projection/query checks.
2. Add `tests.smoke.graph_quality_smoke_test` and `tests.smoke.self_metrics_smoke_test` to the wrapper if they are still omitted, so graph/self-metrics validation is coherent.
3. Update docs listing the wrapper coverage.
4. Run the full wrapper.

---

## PR 2 — `gpc verify`: one command for operator diagnostics

**Objective:** Add a single bounded diagnostic command that explains whether a project is healthy across Postgres, Qdrant, Ollama, Neo4j, Graphify, index state, and MCP-readiness.

### Task 2.1: Define the verification result model

**Files:**
- Create: `gpc/verify.py`
- Test: `tests/test_verify.py` or `tests/smoke/verify_smoke_test.py`

**Behavior:**
- `VerificationCheck`: name, status (`pass`, `warn`, `fail`, `skip`), message, remediation.
- `VerificationReport`: project_slug, checks, summary counts.
- Pure data model should be testable without live services.

**RED command:**

```bash
./venv/bin/python -m pytest tests/test_verify.py -q
```

### Task 2.2: Implement offline checks

**Files:**
- Modify: `gpc/verify.py`
- Test: `tests/test_verify.py`

**Checks:**
- project path exists;
- `.gpc.yaml` or registry project can resolve;
- latest index run exists;
- indexed files/chunks/points are non-zero when project is expected to be indexed;
- Graphify report file exists when `graphify-out/` is present.

### Task 2.3: Implement live checks

**Files:**
- Modify: `gpc/verify.py`
- Test: `tests/smoke/verify_smoke_test.py`

**Checks:**
- Postgres connection;
- Qdrant collection reachable;
- Ollama embedding model reachable;
- Neo4j reachable;
- graph summary available or clear warning with remediation.

### Task 2.4: Add CLI command

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/verify_smoke_test.py`

**CLI:**

```bash
gpc verify --project gpc
gpc verify --project gpc --quick
gpc verify --project gpc --live
gpc verify --project gpc --json
```

### Task 2.5: Document `gpc verify`

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/scripts.md`

**Acceptance:** docs show when to use `doctor` vs `verify`.

---

## PR 3 — Stale context detector

**Objective:** Detect when Postgres/Qdrant/Neo4j/Graphify are stale relative to the Git working tree or latest commit.

### Task 3.1: Add staleness model

**Files:**
- Create: `gpc/staleness.py`
- Test: `tests/test_staleness.py`

**Behavior:**
- compare Git-tracked files with indexed files;
- ignore untracked-but-ignored or generated paths such as `.git/`, `venv/`, `.gpc/`, `__pycache__/`, and `graphify-out/` except for `graphify-out/GRAPH_REPORT.md` timestamp checks;
- detect modified files since latest index run;
- detect deleted files still present in index;
- detect Graphify report older than latest code change;
- return remediations, not automatic destructive actions.

### Task 3.2: Add CLI surface

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/test_staleness.py`

**CLI:**

```bash
gpc status --project gpc --staleness
gpc stale --project gpc --json
```

### Task 3.3: Integrate with `gpc verify`

**Files:**
- Modify: `gpc/verify.py`
- Test: `tests/test_verify.py`

**Behavior:** stale context should be a `warn`, not a hard `fail`, unless the project has zero usable context.

---

## PR 4 — Search evaluation harness

**Objective:** Add repeatable quality checks for semantic retrieval so changes to chunking, embeddings, or filters cannot silently degrade search.

### Task 4.1: Add eval fixture format

**Files:**
- Create: `tests/fixtures/search_eval_gpc.yml`
- Create: `gpc/search_eval.py`
- Test: `tests/test_search_eval.py`

**Fixture shape:**

```yaml
embedding:
  provider: ollama
  model: nomic-embed-text:latest
  dimensions: 768
queries:
  - query: "how does MCP stay read-only?"
    expected_paths:
      - docs/architecture.md
      - SECURITY.md
      - gpc/mcp_server.py
```

### Task 4.2: Implement metrics

**Files:**
- Modify: `gpc/search_eval.py`
- Test: `tests/test_search_eval.py`

**Metrics:**
- recall@k;
- expected file hit rate;
- missing expected paths;
- latency per query;
- provider/model metadata.

### Task 4.3: Add CLI command

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/search_eval_smoke_test.py`

**CLI:**

```bash
gpc eval-search --project gpc --fixture tests/fixtures/search_eval_gpc.yml --k 5
gpc eval-search --project gpc --fixture tests/fixtures/search_eval_gpc.yml --json
```

### Task 4.4: Add baseline queries

**Files:**
- Modify: `tests/fixtures/search_eval_gpc.yml`

**Initial queries:**
- MCP read-only contract;
- project/repo registry model;
- Qdrant payload filtering;
- Graphify cross-repo bridge confidence;
- self metrics/drift signals.

---

## PR 5 — Context packs

**Objective:** Export compact task-specific context packages for agents, issues, or reviews.

### Task 5.1: Add context pack builder

**Files:**
- Create: `gpc/context_pack.py`
- Test: `tests/test_context_pack.py`

**Behavior:**
- takes project, optional repo, query, max chunks, include_graph;
- returns metadata, selected chunks, source paths, graph neighbors if available, token estimate.

### Task 5.2: Add Markdown output

**Files:**
- Modify: `gpc/context_pack.py`
- Test: `tests/test_context_pack.py`

**Output sections:**
- project summary;
- query;
- selected context;
- graph notes;
- validation commands;
- known warnings.

### Task 5.3: Add CLI command

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/context_pack_smoke_test.py`

**CLI:**

```bash
gpc context-pack --project gpc --query "MCP architecture" --format markdown
gpc context-pack --project gpc --query "MCP architecture" --include-graph --output /tmp/gpc-context.md
```

---

## PR 6 — Health report/dashboard

**Objective:** Produce an executive project health report from existing GPC data.

### Task 6.1: Add health report data aggregator

**Files:**
- Create: `gpc/health_report.py`
- Test: `tests/test_health_report.py`

**Data sources:**
- project/repo registry;
- index status;
- self metrics;
- drift signals;
- MCP usage;
- graph summary when available.

### Task 6.2: Add CLI output formats

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/health_report_smoke_test.py`

**CLI:**

```bash
gpc health-report --project gpc
gpc health-report --project gpc --json
gpc health-report --project gpc --markdown
```

### Task 6.3: Optional static site export

**Files:**
- Modify/Create: `site/health.html` only if static export is explicitly requested later.

**Note:** Defer this until CLI report is stable.

---

## PR 7 — MCP usage audit improvements

**Objective:** Turn raw MCP observability into actionable local usage reports without logging secrets.

### Task 7.1: Extend usage aggregation

**Files:**
- Modify: `gpc/mcp_observability.py`
- Test: `tests/test_mcp_observability.py`

**Metrics:**
- calls by client;
- calls by tool;
- errors by tool;
- average latency if available;
- top projects;
- no raw secret-bearing payloads;
- redaction test coverage for token/password/secret/api-key-like values before aggregation output.

### Task 7.2: Add CLI command/options

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/mcp_observability_smoke_test.py`

**CLI:**

```bash
gpc mcp-usage --since 24h --by-client --by-tool
gpc mcp-usage --project gpc --json
```

### Task 7.3: Keep MCP read-only query bounded

**Files:**
- Modify: `gpc/mcp_server.py` only if needed
- Test: `tests/smoke/mcp_smoke_test.py`

**Rule:** MCP can expose aggregate read-only usage. It must not expose raw sensitive payloads.

---

## PR 8 — Safe maintenance diagnostics

**Objective:** Add dry-run maintenance checks for orphaned or inconsistent read-model data.

### Task 8.1: Add maintenance diagnostics module

**Files:**
- Create: `gpc/maintenance.py`
- Test: `tests/test_maintenance.py`

**Checks:**
- Qdrant points without Postgres chunks;
- chunks without files;
- files for missing projects/repos;
- Neo4j nodes without `project_slug`;
- duplicate aliases;
- stale self metrics references.

### Task 8.2: Add dry-run CLI

**Files:**
- Modify: `gpc/cli.py`
- Test: `tests/smoke/maintenance_smoke_test.py`

**CLI:**

```bash
gpc maintenance doctor --project gpc
gpc maintenance prune-stale --project gpc --dry-run
```

**Safety:** no destructive action without explicit `--yes`, and destructive implementation should be a separate PR.

---

## PR 9 — Test maturation and hotspot refactor preparation

**Objective:** Add enough unit coverage to safely refactor `gpc/cli.py`, `gpc/indexer.py`, and MCP/graph helpers.

### Task 9.1: Add parser snapshot/unit tests

**Files:**
- Test: `tests/test_cli_parser.py`

**Behavior:** verify important commands/options parse correctly without hitting live services.

### Task 9.2: Add indexer unit seams

**Files:**
- Test: `tests/test_indexer_discovery.py`
- Test: `tests/test_indexer_chunking.py`

**Behavior:** verify ignore policy, chunk boundaries, payload fields, stable IDs.

### Task 9.3: Add MCP payload tests

**Files:**
- Test: `tests/test_mcp_payloads.py`

**Behavior:** verify error payload shape, repo filter normalization, bounded results.

### Task 9.4: Refactor only after tests are green

**Files:**
- Modify later: `gpc/cli.py`, `gpc/indexer.py`, `gpc/mcp_server.py`

**Rule:** no broad refactor until the above tests exist, pass, and establish at least minimal per-module coverage over the module being refactored.

---

## Full validation checklist

Run before reporting any PR as complete:

```bash
git status --short
./venv/bin/python -m compileall -q gpc scripts tests
./venv/bin/python -m ruff check --select E9,F63,F7,F82 gpc scripts tests
changed_py=$(git diff --name-only -- '*.py')
if [ -n "$changed_py" ]; then
  printf '%s\n' "$changed_py" | xargs ./venv/bin/python -m ruff check
  printf '%s\n' "$changed_py" | xargs ./venv/bin/python -m black --check
fi
./scripts/run_smoke_tests.sh
```

For graph-related PRs, additionally run:

```bash
./venv/bin/python -m tests.smoke.graph_projection_smoke_test
./venv/bin/python -m tests.smoke.graph_query_smoke_test
./venv/bin/python -m tests.smoke.graph_quality_smoke_test
```

For retrieval-related PRs, additionally run:

```bash
./venv/bin/python -m tests.smoke.embedding_smoke_test
./venv/bin/python -m tests.smoke.search_test
```

After validation, refresh dogfood context:

```bash
gpc-index . --slug gpc --name GPC --description 'Global Project Context infrastructure'
graphify update .
```

Then verify:

```bash
gpc status --project gpc
```

---

## Recommended implementation order

1. PR 1 — Graph self-visibility.
2. PR 2 — `gpc verify`.
3. PR 3 — stale context detector.
4. PR 4 — search evaluation harness.
5. PR 5 — context packs.
6. PR 6 — health report/dashboard.
7. PR 7 — MCP usage audit improvements.
8. PR 8 — safe maintenance diagnostics.
9. PR 9 — test maturation and hotspot refactor preparation.

This order deliberately fixes correctness/diagnostics before adding larger product surfaces.
