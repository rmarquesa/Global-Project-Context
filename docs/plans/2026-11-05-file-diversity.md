# Optional one-per-file selection after hybrid fusion

Status: implementation, source-reviewed new cases, integrated unit checks and
isolated new-case validation complete. File diversity increased, but no new-case
file-hit recall gain was observed. Defaults are unchanged.

## Discovery evidence reused, not rerun

The [weighted experiment](2026-11-05-weighted-lexical.md) already captured complete
bounded candidate pools and computed offline caps. Inspection of those retained
results gives the following weighted-hybrid hit counts at k=5:

| Development fixture | Existing output | Offline cap 1 | Offline cap 2 |
| --- | --- | --- | --- |
| Existing four queries | 4 / 10 | 6 / 10 | 4 / 10 |
| Context twelve queries | 9 / 19 | 11 / 19 | 9 / 19 |
| Paired sixteen queries | 12 / 16 | 14 / 16 | 14 / 16 |

Both caps recover the paired Portuguese installer case, whose expected file was
already present in the pool. Natural-Portuguese hits in that paired set change
from 0/4 to 2/4. Cap 1 also recovers `gpc/mcp_server.py` for the earlier MCP
read-only case, but **not** `docs/architecture.md`, which is absent from its pool.
This does not solve candidate omissions.

These are discovery measurements already used to choose this policy, not an
independent test. They motivate the least invasive experiment: preserve RRF and
its scores, selecting distinct files afterward rather than inventing new fusion
weights. No index or model call was made for this inspection.

## Fixed selection contract

- Add `file_diversity="one-per-file"` as an explicit hybrid-only Python/evaluation
  option; default `none`. CLI: `eval-search --file-diversity one-per-file`.
- Iterate the existing fully hydrated ranked union in its current order, taking
  the first chunk from each repository-qualified file until k results are chosen.
- Preserve channel candidates, budgets, RRF scores, task preferences and legacy/
  weighted ordering. Do not enlarge the pool, refill through a backend, pad short
  outputs, or make another embedding/retrieval call.
- A file in two repositories represents two distinct identities. Only existing
  scoped hydration supplies identities; rejected candidates cannot leak metadata.
- Reject the nondefault option for a single semantic or lexical evaluation before
  services. In comparisons, apply it only to hybrid; semantic and lexical remain
  unchanged. Do not add MCP/global search defaults.
- Record the nondefault policy and ordered selected chunk IDs in a content-free
  version-3 capture, retaining the entire ranked pool for analysis. Default
  legacy v1 and weighted v2 captures remain unchanged. Selected results need not
  be a prefix of the pool under v3.
- Reuse a pure selection helper for offline cap replay and actual result
  selection, so the two implementations cannot silently diverge.

## New development cases, authored independently

A separate owner authors six new EN/PT pairs (twelve queries) against the frozen
source corpus, without inspecting scoring/output changes or the known loss
cases. Four pairs use natural phrasing; two carry the same explicit source
identifier/path in both languages. Expected paths, tasks and pair identities are
fixed and source-reviewed before evaluation.

These labels are agent-authored/reviewed development judgments, not human gold or
a representative held-out benchmark. They are new relative to the discovery
queries but remain on the same small repository corpus. Query/label selection
must not be changed after seeing outcomes.

The reviewed fixture is `tests/fixtures/search_eval_diversity.yml`, SHA-256
`8c96fb6e641db0d2bc519a93ae354eab85151514ce1a0ee8695c4a2c6382e7b6`.
It has sixteen expected-label occurrences across six distinct files, all verified
against both included and indexable paths in the frozen manifest. Embedding and
confidence topics recur between natural and identifier pairs; this is not six
independent domains. Before measurement, source review clarified the retention
preview question to distinguish it from the maintenance doctor's diagnostics.
All translations and source anchors were reviewed; two integrity/parser tests,
query bounds, Ruff and Black passed.

## Acceptance and functional ownership

One implementation owner handles the pure helper, search selection, capture
versioning, evaluator/CLI plumbing and regression tests. A separate fixture owner
handles only its three new fixture/provenance/test files. The coordinator reviews
contracts and labels, owns documentation and integrates checks.

Acceptance requires unchanged default outputs/schemas, unchanged channel counts
and budgets, score-preserving ordered selection, no refill on short results,
repository-aware identity, filtered-metadata privacy, correct v3 provenance and
baseline metrics, and semantic/lexical invariance in comparisons. Tests run with
backing services disabled and with/without optional PyYAML.

After acceptance, reuse the retained old captures offline. Live validation needs
only the new queries, on one owned isolated index of the same frozen corpus.
Compare `none` and `one-per-file` within that index, for weighted hybrid and
legacy hybrid; verify matching candidate pools and backend counts. Preserve
production snapshots and remove only owned resources. No repeat of the completed
weighted SQL experiment or old-fixture live evaluation is required.

### Completed offline compatibility check

The coordinator independently replayed all **64 retained hybrid captures** from
the preceding legacy/weighted experiment using a simple first-unique-file oracle.
The new shared selector and replay helper matched both the oracle and the already
recorded cap-1 paths exactly. The source capture bundle remained byte-identical;
no backend or model calls were made. This validates compatibility with previously
recorded selection, not general relevance.

Full-suite validation passed with all backing-service endpoints unavailable:
**867 passed**, four integration tests excluded, with PyYAML; **861 passed**,
six PyYAML-only cases skipped and four integration tests excluded, with its import
blocked. Ruff/Black passed seven changed/new Python files; curated Mypy passed
five modules. The coordinator reviewed selection, capture-version and comparison
integration. No SQL, lexical scoring or RRF change is part of this diff.

## Interpretation limits

File-label recall cannot measure whether several chunks from one file provide
necessary complementary evidence. A one-per-file policy may improve source
coverage while reducing answer completeness; fewer than k unique files produces
a deliberately short response. Default promotion requires broader evidence than
these tests. No embedding-model, lexical-score, RRF, AST or learned-reranker
change is part of this increment.

Under these **file-level** judgments, keeping the first occurrence of each file
from the same ordered pool cannot remove a file that appeared in the uncapped
top-k: it retains those files and may add others. Nondecreasing file-hit recall
is therefore partly a property of the selection rule and metric, not an
independent quality discovery. It says nothing about whether dropping additional
chunks removes evidence required for an answer. New-case gains must be reported
with this limitation rather than used alone to justify default promotion.

## Completed new-case runtime validation

One isolated index contained **104 files and 320 chunks**, with zero errors and
exact PostgreSQL/Qdrant parity. Every expected path was chunk-backed. Four CLI
calls produced eight reports and 96 searches, with exactly 72 semantic-channel
and 72 lexical-channel calls. No old fixtures or completed weighted SQL contract
tests were rerun.

The fixed matrix used only the new twelve-case fixture at k=5:

- A: weighted `--compare-all`, default selection.
- B: the same, with `--file-diversity one-per-file`.
- C: legacy `--mode hybrid`, default selection.
- D: the same, with `--file-diversity one-per-file`.

### Hybrid results

| Variant | Pool hits | Top-five hits | Recall@5 | MRR@5 | nDCG@5 | Unique-file occurrences |
| --- | --- | --- | --- | --- | --- | --- |
| Weighted / default | 11 / 16 | 11 / 16 | .687500 | .708333 | .676201 | 36 / 60 |
| Weighted / one-per-file | 11 / 16 | 11 / 16 | .687500 | .708333 | .678440 | 60 / 60 |
| Legacy / default | 11 / 16 | 8 / 16 | .500000 | .259722 | .328960 | 52 / 60 |
| Legacy / one-per-file | 11 / 16 | 8 / 16 | .500000 | .287500 | .350781 | 60 / 60 |

All queries returned five results, so short-output behavior was covered by unit
tests rather than observed in this live fixture. Unique-file occurrences sum
distinct repository-qualified files **within each query**, not globally.

There were **zero file-hit wins, zero losses and twelve equal queries** for each
selection comparison. Weighted diversity changed one English natural-query nDCG;
legacy diversity improved rank metrics without recovering the three additional
expected occurrences present in its pool. The discovery-fixture hit gains did
not reproduce as a recall gain in these new cases.

| New-case slice | Weighted hits, both selections | Legacy hits, both selections |
| --- | --- | --- |
| English | 8 / 8 | 6 / 8 |
| Portuguese | 3 / 8 | 2 / 8 |
| Natural phrasing | 7 / 12 | 5 / 12 |
| Explicit identifiers | 4 / 4 | 3 / 4 |

Weighted Portuguese pool coverage was also 3/8: all available expected occurrences
already appeared in top-five. Diversity cannot supply the five absent occurrences.
Legacy Portuguese pool coverage was 4/8, but both selections recovered only 2/8.

### Behavior, provenance and safety

- A/B semantic and lexical reports were identical except latency. Hybrid A/B and
  C/D had identical channels, full ranked pools, scores, task ordering and budgets.
  Returned selected IDs/scores matched their preserved ranked entries.
- All 96 capture hashes passed independent checks: 36 v1, 36 v2 and 24 v3. The
  version-3 selected paths/IDs matched an independent first-unique-file oracle,
  and cap-1 replay matched the actual selection. The coordinator additionally
  checked all 24 diverse cases against their unchanged pools.
- Raw capture-bundle SHA-256:
  `ee989a06e2919ac04fb34bbf5ec2301a283d6f1f31a6b6fc6dde6e00a322cfd7`.
- Indexed source remained `102cabf`; current application code executed from the
  checkout, with all 41 module hashes unchanged. Frozen fixture and all 107
  source-blob hashes were unchanged.
- Production's 19 table counts, registry, migration checksums, 478 vector points
  and container/image/mount identities matched fresh before/after snapshots.
  The owned database and vector container were removed and verified absent.
- Existing entity-extraction/self-metrics suppressions and forbidden Neo4j access
  were retained. No model download, production reindex/rebind or service change.
- Two offline verifier assumptions were corrected from saved outputs (lexical
  embedding metadata and JSON-normalized version-count keys). Raw outputs stayed
  unchanged; there was no operational retry, reindex or evaluation rerun.
- Code graph maintenance: 2,286 nodes, 5,996 edges, 116 communities. Protected
  artifacts remain local. Fixed-order, instrumented timings are not performance
  or scalability evidence.

## Decision

Publish the option as an explicit experiment, not a new default. It successfully
enforces per-file selection and improves breadth, but the new fixture adds no
file-hit recall gain, and answer-evidence completeness remains unmeasured. The
remaining natural-Portuguese candidate omissions cannot be solved by this filter.
Before further ranking changes or default promotion, use separately reviewed
answer-evidence judgments and investigate missing candidates rather than treating
more unique files as sufficient proof of better answers.
