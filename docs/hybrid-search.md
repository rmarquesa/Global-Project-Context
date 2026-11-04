# Hybrid retrieval and task-aware context

The default remains `semantic`: embeddings and Qdrant retrieve candidates, and
Postgres supplies their source text. Two opt-in modes complement that baseline:

| Mode | Candidate retrieval | Dependencies |
| --- | --- | --- |
| `semantic` | Vector similarity | Postgres, embeddings, Qdrant |
| `lexical` | Full text, literal identifiers and paths | Postgres |
| `hybrid` | Lexical and semantic candidates fused by rank | All of the above |

These modes search already indexed chunks. They do not read arbitrary source
files, update an index, regenerate Graphify, or send code to a new service.

## Usage

```bash
gpc search 'index_project_path' --project gpc --mode hybrid
gpc search 'gpc/registry.py' --project gpc --mode lexical
gpc search 'retry after an indexing failure' --project gpc --mode hybrid --task debug
gpc search 'authentication' --project product --repo api --repo web --mode hybrid

gpc context-pack 'test repository isolation' --project gpc \
  --mode hybrid --task test --max-chars 12000 --output context.md
```

MCP `gpc_search` and `gpc_context` accept the same `mode` and `task` arguments.
Existing calls without those arguments retain semantic retrieval. The existing
`include_graph` option adds structural annotations **after** retrieval; it is not
the lexical/vector fusion switch.

## Ranking and scope

Hybrid mode uses reciprocal rank fusion rather than directly adding incompatible
vector and lexical scores. Results carry `retrieval_sources` and `ranking_signals`
to distinguish candidate sources and task preferences. Scores must not be compared
across modes or interpreted as confidence probabilities.
The reported score is the raw retrieval/fusion score; a 5% task preference can
change ordering without rewriting that score. Consult `ranking_signals` rather
than assuming every displayed list is sorted by its raw score.

Tasks are explicit preferences: `general`, `debug`, `implement`, `review` and
`test`. In lexical/hybrid mode, lightweight path/type heuristics favor relevant
implementation, configuration or test chunks. They do not infer a task with an
LLM, guarantee the best context, or replace a learned reranker. Semantic ordering
remains the baseline. Identifier matching is textual, not an AST symbol index.

Every candidate source and source-text hydration is scoped to the resolved
project and requested repositories. Repository identity comes from Postgres,
not a vector payload. Identical paths in different repositories remain distinct.
Backend failures are errors rather than an undisclosed downgrade from hybrid to
one candidate source; explicitly select lexical mode when embeddings are offline.

Candidate and query sizes are bounded. This first version does not promise
complete recall for very large corpora or broad queries. Literal/path matching
can cost more than indexed full-text matching; benchmark representative projects.
Queries accept at most 2,048 characters and 32 terms. The core search API accepts
1–100 results, with up to three times that many candidates per retrieval channel;
MCP and context composition retain their tighter existing limits. Chunks without
an attributable registered repository, or with inconsistent project/repo ownership,
are excluded rather than trusting vector metadata to fill the gap.

## Migration

`migrations/0010_lexical_search.sql` adds a full-text search index. Apply migrations
through the normal explicit `gpc migrate` workflow after reviewing the deployment
impact. Creating an index can consume storage and block writes. Neither retrieval
nor this implementation session applies migrations automatically.

## Evaluate rather than assume

Use the same labeled queries and project snapshot for both modes:

```bash
gpc eval-search --project my-project --fixture my-eval.yaml --k 5 --compare --json
gpc eval-search --project my-project --fixture my-eval.yaml --mode lexical --json
```

Reports include recall, query hit rate, reciprocal rank, nDCG and latency. Review
individual misses as well as aggregates. Use repository-qualified expected paths
(`api:src/auth.py`) when a path alone is ambiguous. A fixture's repo/task filters
must match the context intended for that query.

For example, save a project-specific fixture as `my-eval.yaml`:

```yaml
queries:
  - query: "validateSession"
    repo: api
    task: debug
    expected_paths:
      - "api:src/auth.py"
      - "api:tests/test_auth.py"
```

Unqualified expected paths retain the legacy path-only meaning. Prefer qualified
labels for multi-repository evaluations. Compare runs on a quiet checkout with no
concurrent indexing; the command does not freeze backend state. Its sequential
timings can also be affected by warmed caches, so repeat controlled runs before
making latency claims.

Without optional PyYAML, the loader supports a restricted mapping/list/scalar
fixture syntax and rejects unsupported constructs. Keep fixtures simple like the
example above; YAML anchors and custom tags are not required.

The included synthetic fixture and unit tests validate behavior and metric
arithmetic; they are **not** a measured production quality improvement. Live-store
SQL performance, edit-to-search freshness, representative relevance and answer
correctness remain separate validation work. Fewer returned characters alone do
not demonstrate a better answer. Existing context-pack character budgets and
citation contracts still apply.
