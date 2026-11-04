# Retrieval evaluation diagnostics

Status: implemented; fixture judgments are developer-authored and not yet
human-reviewed or representative of production requests. Runtime evidence below
is separate from any production-quality claim.

## Why this increment

The first four-query local evaluation returned recall@5 0.10 and MRR@5 0.0625.
A subsequent read-only check found all nine distinct expected paths in the
registered project's PostgreSQL index, each with chunks. Missing files alone do
not explain that result. Neither this check nor the metrics establish vector
completeness, source freshness, answer correctness, or a particular ranking defect.

The registered project still points to the older worktree. It must not be silently
rebound or reindexed to improve an evaluation score.

An independent read-only, parameterized SQL check of the new twelve-case fixture
found **13 distinct expected files: seven with chunks and six not indexed**.
The absent files were `docs/decision-memory.md`, `docs/hybrid-search.md`,
`docs/watcher.md`, `gpc/decisions.py`, `gpc/index_lock.py` and `gpc/watcher.py`.
No existing expected file was present without chunks. This is evidence of a
coverage gap for the new development cases, not evidence that any retrieval mode
is better. The old four-query fixture and this new fixture exercise different
expected sources and must not be compared as if they were the same benchmark.

## Deliverables and ownership

| Area | Deliverable | Acceptance |
| --- | --- | --- |
| Evaluator and CLI | Opt-in corpus diagnostics and semantic/lexical/hybrid comparison | Preserve existing metrics and default output; parameterized scoped reads; no content writes |
| Fixture | Twelve source-grounded Portuguese/English cases with qualified labels | Existing parsers agree; expected files exist; rationales and limitations documented |
| Runtime validation | Read-only comparison on the existing local index | Record fixture/corpus identity, coverage and per-mode misses; no reindexing or ranking changes |
| Product documentation | Usage guide, website and roadmap | Clearly distinguish implementation, measured local evidence and unvalidated quality |

## Interpretation contract

- Missing sources remain in the original recall denominator. Diagnostics must not
  improve the score by excluding difficult labels.
- A PostgreSQL file with chunks is not proof that its vectors exist or its indexed
  contents match disk. Availability and retrieval success are separate evidence.
- Unqualified duplicate paths can be ambiguous; project and repository filters
  must use the same identity semantics as retrieval.
- Invalid scopes or database failures are evaluation errors, not zero-quality
  answers. Opt-in diagnostics must not silently disappear on failure.
- Comparison runs share fixture and diagnostic inputs, but do not freeze the live
  search backends. Concurrent indexing and cache effects limit comparability.
- Developer-authored cases are a development set, not a held-out benchmark.
  Labels require human review and real-user queries before optimizing against them.
- No LLM judge, agent benchmark or paid model run is part of this increment.

AST retrieval, learned reranking and connectors remain separate subsequent work.

## Implementation checks

- **738 unit tests passed**, with four explicit service integration tests
  deselected. The run overrode all backing-service endpoints to unavailable
  loopback ports.
- Ruff and Black passed all seven changed/new Python files; curated Mypy passed
  five modules. CLI help exposed the mutually exclusive comparison options.
- An independent scoped review found no introduced isolation or correctness
  defects; its 87 targeted checks also ran with unavailable service endpoints.
- Code graph maintenance completed: 2,114 nodes, 5,529 edges, 127 communities.
- This increment changes evaluation CLI/reporting, not indexing, ranking, MCP
  tools or migrations. The existing HTTP container was not rebuilt.

## Measured local results (k = 5)

The completed comparisons used the unchanged registered index. These are
development-fixture measurements, not a representative benchmark or a claim of
improvement. Compare modes **within a fixture**, not across different query sets.

| Fixture | Mode | Hits / expected labels | Recall@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- | --- |
| Existing four queries | Semantic | 1 / 10 | 0.10 | 0.0625 | 0.066017 |
| Existing four queries | Hybrid | 0 / 10 | 0 | 0 | 0 |
| Existing four queries | Lexical | 0 / 10 | 0 | 0 | 0 |
| New twelve queries | Semantic | 0 / 19 | 0 | 0 | 0 |
| New twelve queries | Hybrid | 0 / 19 | 0 | 0 | 0 |
| New twelve queries | Lexical | 0 / 19 | 0 | 0 | 0 |

Diagnostics agreed with independent SQL. The old fixture's ten expected-label
occurrences were all chunk-backed. The new fixture had ten chunk-backed and nine
absent occurrences (seven available and six absent **distinct files**). Coverage
alone therefore does not explain all misses. No labels were removed and no
reindexing or ranking adjustment was made to improve these scores.

The fixture and metadata fingerprints were identical across modes within each
comparison. Metadata fingerprints cover fixture-label paths only, not the entire
searchable corpus:

| Fixture | Fixture SHA-256 | Metadata SHA-256 |
| --- | --- | --- |
| Existing | `85d8f7feee818db5e8c08857b8df454d2d4e2a3245d395e3983d7d6f8f129589` | `c5099c0c614c2d5d4cf15ab7faeba9aea6da6db2c09a20584fbc6d3193047eec` |
| New | `f1ea536a558f66cfb3edddcfc6129e2ac93277e1ab0260fd9f8dde14b8d75cb8` | `ccd25af1ce0f67ec8a2176f2c76503b8570dda268e98a043789c307ee6949abd` |

A lexical-only diagnostic run also passed with Ollama and Qdrant endpoints
overridden to unavailable loopback ports. It reported embedding `not_applicable`,
dimensions zero, and the same lexical rankings, quality metrics and diagnostics.
All 19 public-table counts were unchanged across that run. No contemporaneous
SQL baseline was captured before the first two comparison runs; later counts
matched the historical baseline but cannot retroactively prove before/after
invariance for those first runs.

An initial offline-test invocation mistakenly retained `--compare-all`, so it
correctly attempted semantic embedding and failed with Ollama unavailable. The
test invocation was corrected to `--mode lexical`; no product-code correction
was necessary. Original failure evidence and successful JSON reports are retained
in a protected local artifact directory, not published with indexed content.

## Next decision

Keep semantic retrieval as the default. The observed hybrid/lexical scores do
not justify changing it. Review candidate relevance labels, then evaluate a
current, isolated corpus and inspect candidate generation/per-file diversity
before selecting an AST or reranking implementation. Do not silently redirect
the user's existing worktree registration or tune against these twelve cases
as if they were a held-out test set.
