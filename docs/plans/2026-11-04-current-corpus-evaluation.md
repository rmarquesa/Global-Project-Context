# Current-corpus retrieval experiment

Status: completed after one harness correction. All six evaluations, production
invariants and owned-resource cleanup passed. No ranking change was made.

## Question

How do the existing semantic, hybrid and lexical modes behave on a current,
isolated corpus when evaluation fixtures and their provenance are excluded?

The [previous evaluation](2026-11-04-retrieval-diagnostics.md) used the unchanged
registered worktree index. It identified missing expected files as well as misses
for available files. Inspection of returned paths also found the legacy fixture
itself among lexical/hybrid results. These observations motivate a controlled
corpus experiment, not an immediate ranking change.

## Fixed protocol

- Source revision: `102cabf`; materialize tracked files outside the working tree.
- Exclude `tests/`, `docs/plans/`, `docs/retrieval-evaluation.md`, `site/` and
  `graphify-out/` before indexing. Do not index Git metadata, untracked files,
  environment secrets or symlinks. Preserve native indexer safety checks.
- Record included file hashes, excluded paths and reasons, source revision and
  embedding model before scoring. Assert that every expected path remains in
  the materialized corpus; exclusions must not depend on measured scores.
- Use a newly owned PostgreSQL database and separate Qdrant container. An
  isolated PostgreSQL database alone is insufficient because the vector
  collection name is shared by configuration defaults.
- Disable graph extraction and operational metrics collection for indexing.
  Use the already installed local embedding model; do not download models.
- Index once, then evaluate both unchanged fixtures at k=5 using
  `--compare-all --diagnose`. Load fixtures from outside the indexed corpus.
- Capture production table counts, repository registration, vector count and
  container/volume identity before and after. Remove only owned experimental
  resources and preserve protected reports and manifests.

## Interpretation

These labels remain developer-authored development judgments, not a held-out
or human-approved benchmark. Compare retrieval modes within the fixed isolated
corpus. A difference from the earlier registered-index results cannot establish
an algorithmic improvement: corpus freshness and composition have changed.

Report coverage, hits, recall, MRR, nDCG and repeated-file occupancy separately.
Full coverage does not establish that judgments are complete or that retrieved
chunks provide useful evidence. Fixed-order timings are cache-biased. Keep
semantic retrieval as the default until stronger evidence supports a change.

## Ownership and acceptance

The infrastructure specialist owns the external harness, manifest, isolated
services, comparisons and cleanup. A read-only research specialist independently
traces candidate generation and ranking in `gpc/retrieval.py` and `gpc/search.py`.
The coordinator reconciles artifacts and documentation; neither specialist edits
product code. No production reindexing, ranking modification or migration is part
of this experiment.

Completion requires reproducible corpus identity, explicit isolation guards,
all expected paths chunk-backed, all six mode/fixture reports, unchanged
production invariants and verified cleanup. Any failed or unrun checks must be
reported rather than inferred from successful command exit codes.

## Candidate-generation review

A focused read-only review, independently checked against the current SQL and
search function, established these existing behaviors:

- `gpc/retrieval.py:query_terms` preserves Unicode and identifier/path tokens
  without application-level stopword removal or stemming.
- `lexical_candidates` uses `plainto_tsquery('simple', query)`: full-text terms
  are combined with AND, not an adjacency/phrase requirement. A separate OR
  condition admits chunks matching **any** literal term in path, title or content.
- The literal branch adds a single `1.0` bonus whether one or several terms
  match. If full-text rank is zero, literal-only candidates tie and are ordered
  by chunk ID. Common natural-language words may therefore admit many equally
  scored candidates; this is a testable hypothesis, not an established cause of
  the recorded misses.
- At k=5, `search_project_context` requests at most 15 candidates per channel.
  Hybrid uses reciprocal rank fusion with constant 60. Task preferences adjust
  lexical/hybrid ordering by 5%, after candidate generation.
- Repeated chunk IDs are deduplicated within each channel, but distinct chunks
  from one file may occupy multiple final slots. Aggregate distinct paths across
  different queries do not prove harmful duplication within an individual query.

The smallest follow-up diagnostic is to measure expected-path coverage at each
channel cutoff, then after fusion/final top-k, together with within-query unique
file counts and lexical score ties. This separates candidate omission from
ordering or file-occupancy effects before choosing AST retrieval or reranking.
No retrieval code or defaults were changed during this review.

## First attempt: harness failure, no relevance scores

The first attempt materialized 107 included files and 83 exclusions; 104 files
were indexable. Native indexing completed with 320 chunks, zero file errors and
exact PostgreSQL/Qdrant identifier parity. All expected paths in both fixtures
were eligible and chunk-backed.

Evaluation then stopped before retrieval: an external ownership-check SELECT
had already opened a transaction when diagnostics requested repeatable-read
isolation. PostgreSQL rejected the transaction change. This is interference from
the experimental harness, not an observed failure of ranking or the normal CLI.
Both fixtures' scores are unavailable for this attempt.

The owned database and vector container were removed and their absence verified.
All 19 production table counts, repository registrations, migration checksums,
478 vector points and container/image/mount identities were unchanged.

At this revision `IndexOptions` has no graph/metrics suppression flags. The
external harness therefore used counted no-ops for entity extraction and metrics
collection and forbade Neo4j acquisition. Native chunk/vector indexing and
ranking remained unchanged; this is not a test of graph extraction or metrics.

The correction must move ownership checks off the application's connection,
preserving transaction boundaries. A new diagnostic transaction preflight is
required before another index run. The second attempt must use the same source,
included-file contents and exclusions, preserve the first failure artifacts, and
capture a fresh production baseline before creating new owned resources.

## Corrected experiment results

Transaction-neutral ownership checks and the native diagnostic preflight passed
before embedding. The repeated experiment retained the same included-file
contents, exclusions and fixture bytes. Only the synthetic experiment Git commit
changed; the actual source revision remained `102cabf`. The manifest SHA-256 is
`97f44a5c479e5caa053e7da1229ec0f055c7071c9d94dd6da17425d33ade17b7`.

The corrected run indexed 104 files into 320 chunks with zero errors and exact
PostgreSQL/Qdrant identifier parity. All ten expected-label occurrences in the
old fixture and all nineteen in the new fixture were chunk-backed. The embedding
model was the existing `nomic-embed-text:latest`, with 768 dimensions and digest
`0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`.

| Fixture | Mode | Hits / expected labels | Recall@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- | --- |
| Existing four queries | Semantic | 4 / 10 | 0.400000 | 0.216667 | 0.260620 |
| Existing four queries | Hybrid | 1 / 10 | 0.100000 | 0.250000 | 0.153287 |
| Existing four queries | Lexical | 0 / 10 | 0 | 0 | 0 |
| New twelve queries | Semantic | 6 / 19 | 0.315789 | 0.333333 | 0.323101 |
| New twelve queries | Hybrid | 4 / 19 | 0.210526 | 0.194444 | 0.193696 |
| New twelve queries | Lexical | 2 / 19 | 0.105263 | 0.166667 | 0.134429 |

All six summaries were independently recomputed from report paths. Each fixture
comparison reused identical diagnostic snapshots across its three modes.
Semantic had higher recall and nDCG in both fixtures, but **not every metric**:
hybrid had higher MRR on the four-query fixture. These small development cases
do not establish general superiority. Differences from the old registered index
are not algorithmic gains because the corpus changed.

### Observed concentration and language confound

In the twelve-case fixture, semantic retrieval returned `docs/roadmap.md` 29 times
across six queries. In five queries it occupied all five slots: cases 3, 5, 7, 9
and 11 (one-based). These five are all Portuguese queries. This is a concrete
within-query concentration finding, but it also confounds file diversity with
query language and embedding behavior; deduplication alone is not a demonstrated
solution. Hybrid returned `gpc/indexer.py` 14 times across the twelve queries;
lexical returned each of `gpc/indexer.py`, `gpc/embeddings.py` and
`gpc/entity_extractor.py` in every query.

### Isolation and publication evidence

All 19 production table counts, repository registrations, migration checksums,
478 vectors and container/image/volume identities matched the fresh baseline.
The newly owned database and Qdrant container were removed and absence verified.
The original failed attempt remains preserved. Protected local artifacts contain
the manifest, exact external harness, preflight, reports and cleanup evidence;
indexed content and credentials are not published here.

The source commit was pushed successfully. Its [Python CI run](https://github.com/rmarquesa/Global-Project-Context/actions/runs/41924854136)
passed quality/unit checks, graph integration and smoke tests. The corresponding
GitHub Pages deployment also succeeded. These validate implementation behavior,
not retrieval relevance.

## Next bounded experiment

Keep existing defaults. Measure per-query expected-path recall in the candidate
pool versus final top-k, lexical score ties, and unique files per query. Then
test an explicit per-file cap on the **same captured candidate pools**, reporting
short results when there are too few unique files rather than silently fetching
more candidates. Compare English/Portuguese development cases separately without
claiming a language benchmark. This will show whether a diversity experiment is
limited by candidate generation before implementing AST retrieval or a learned
reranker. No diversity gain or production readiness is claimed yet.
