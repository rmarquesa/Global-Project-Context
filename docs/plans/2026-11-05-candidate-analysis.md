# Candidate coverage and fixed-pool diversity analysis

Status: implemented, unit-tested and validated against the isolated fixed corpus.
Offline diversity measurements are recorded below; production ranking is unchanged.

## Motivation and predeclared scope

The [current-corpus experiment](2026-11-04-current-corpus-evaluation.md)
established complete expected-source coverage, but substantial retrieval misses
and repeated chunks from the same file. Candidate generation and final ordering
must be measured separately before implementing AST retrieval or learned
reranking.

This increment adds optional evaluation instrumentation, not a new production
ranking policy. `eval-search --analyze-candidates` will imply corpus diagnostics
and capture the existing candidate pools without widening them or performing
additional retrieval. Existing modes, default payloads and scores remain intact.

## Measurement contract

- Capture each channel's existing bounded candidates and the hydrated, ordered
  union before final top-k. Path metadata must come only from the existing scoped
  PostgreSQL hydration, never untrusted vector payloads.
- Reports must omit chunk content, titles, absolute roots and metadata for
  rejected/out-of-scope candidates. Record rejected counts rather than leaked
  identities. Preserve backend error propagation.
- Report expected-label coverage at the channel and final candidate-pool stages,
  final top-k file occupancy and lexical score ties. Missing corpus labels stay
  in metric denominators.
- Replay per-file caps of **one** and **two** over the same ordered candidate
  pool. File identity includes repository. Do not change scores, enlarge the
  candidate budget, refill through a backend or pad short outputs.
- Compute replay recall, MRR and nDCG with the existing evaluator semantics.
  Replay output is separate from baseline metrics and does not claim latency
  improvement. Fingerprint the content-free capture for offline reproducibility.
- Keep the existing semantic default. Replayed caps are diagnostic experiments,
  not new search/MCP options or a promise of better evidence.

## Language slices of the existing baseline

The source-authored language map is fixed: English cases 1, 2, 4, 6, 8, 10;
Portuguese cases 3, 5, 7, 9, 11, 12 (one-based). The following values were derived
offline from the already captured twelve-case experiment, not a new search run:

| Mode | English hits / expected | Portuguese hits / expected |
| --- | --- | --- |
| Semantic | 6 / 9 | 0 / 10 |
| Hybrid | 3 / 9 | 1 / 10 |
| Lexical | 1 / 9 | 1 / 10 |

Semantic top-five lists for the six Portuguese cases contained respectively
1, 1, 1, 1, 1 and 2 distinct files. These development slices contain different
questions and judgments, **not translated paired queries**. They cannot establish
a general language-performance difference or prove the embedding model is the
cause. The slice is useful for locating cases for subsequent diagnosis only.

## Acceptance and ownership

One implementation owner handles search instrumentation, pure replay helpers,
evaluator/CLI integration and their unit tests. The coordinator owns documentation
and integrated verification. Live testing follows only after compatibility and
scope/privacy checks pass, using separately owned resources and the previously
fixed corpus rather than the production index.

Acceptance includes traced/untraced result equality; unchanged backend call
counts; safe scoped metadata; repository-aware caps; deterministic capture
fingerprints; correct short-result behavior; unchanged legacy denominators and
default schemas; shared fixture/corpus snapshots across comparisons; full unit
suite and focused live isolated checks. Successful tests do not themselves prove
retrieval gains.

## Implementation validation

- `--analyze-candidates` and Python `analyze_candidates=True` are optional and
  imply the existing corpus diagnostics. Normal search/evaluation schemas and
  ranking behavior remain unchanged.
- The parent reviewed the new content-free capture helper, scoped hydration
  integration and reuse of legacy metric calculations for cap replays.
- **781 unit tests passed**, with four live-service integration tests excluded.
  All configured backing-service endpoints were overridden to unavailable
  loopback ports for that run.
- Ruff and Black passed the seven changed/new Python files; curated Mypy passed
  five modules. CLI help documents the implication and unchanged defaults.
- Tests cover trace/no-trace equality and backend-call parity across all modes
  and tasks, filtered metadata privacy, empty/failed captures, finite scores,
  repository-qualified caps, short replays, unchanged denominators and shared
  fixture/corpus snapshots.

The runtime protocol separates the fixed **indexed corpus at `102cabf`** from
the **current application implementation**. Application module paths and hashes
must prove the new evaluator is executing, while corpus hashes must match the
previous experiment. Traced/untraced parity is checked inside the same new index;
prior-run score equality is not assumed because regenerated chunk IDs can affect
tie ordering. All captures are retained for subsequent offline analysis.

Code graph maintenance completed with 2,177 nodes, 5,696 edges and 119
communities. Documentation links and the site's updated CLI example were checked.

### Initial runtime preflight

The first runtime attempt stopped before creating resources or indexing. A strict
file-set guard rejected `.gpc/index.lock`, an operational file generated in the
previous experiment's corpus directory. All 107 manifest-listed source hashes
were correct; this was a harness materialization failure, not an application
failure. Production snapshots were unchanged and no retrieval scores were
produced.

The correction materializes **only manifest-listed source blobs** into a new
directory, with relative-path/symlink checks and per-file hash verification.
Generated locks and Git metadata are not source inputs. Original artifacts remain
untouched; acceptance must distinguish source-content integrity from operational
files created legitimately during indexing.

A second attempt passed manifest materialization but stopped before migrations,
indexing or evaluation: the harness purged cached application modules to reload
configuration, then checked their import locations before re-importing them.
Its owned resources were removed and production snapshots matched. This was also
a harness failure; no candidate metrics were produced. The coordinator reviewed
the module-reload sequence and complete offline report verifier. The final
attempt requires an offline rehearsal of purge/configuration/patch/re-import
ordering before any resource creation, not just a syntax or initial-import check.

## Completed runtime validation

The final attempt passed the offline reload rehearsal and transaction-neutral
preflight, then performed one native index: **104 files, 320 chunks, zero errors**.
All expected paths were chunk-backed. Six traced reports matched six same-run
untraced reports in paths, chunk IDs, scores, metrics and corpus diagnostics;
only measured latency was excluded from equality.

The harness observed 96 search calls, 64 semantic-channel calls and 64
lexical-channel calls, exactly one call per active channel/case. **48 captures
and 96 offline cap replays** passed independent validation. Replays made no
backend or embedding calls. All capture fields matched safe metadata whitelists
and authoritative scoped chunk/path records. Live duplicate/dropped counts were
zero; adversarial dropped/foreign-candidate behavior is covered by unit tests,
not by this live corpus.

| Fixture / mode | Expected occurrences found in pool | Baseline hits@5 | Cap 1 hits@5 | Cap 2 hits@5 |
| --- | --- | --- | --- | --- |
| Existing / semantic | 8 / 10 | 4 / 10 | 4 / 10 | 4 / 10 |
| Existing / hybrid | 9 / 10 | 2 / 10 | 2 / 10 | 2 / 10 |
| Existing / lexical | 3 / 10 | 0 / 10 | 0 / 10 | 0 / 10 |
| New / semantic | 9 / 19 | 6 / 19 | 7 / 19 | 7 / 19 |
| New / hybrid | 12 / 19 | 6 / 19 | 6 / 19 | 6 / 19 |
| New / lexical | 5 / 19 | 2 / 19 | 2 / 19 | 2 / 19 |

Pool coverage is not recall@5: each active channel supplied 15 candidates, and
hybrid examined their union. It describes expected-path presence, not a judged
relevance ceiling. All denominators remain unchanged.

Only the new semantic cap metrics changed: baseline recall@5 **0.315789**, MRR
**0.333333**, nDCG **0.323101**; cap 1 yielded **0.368421 / 0.375000 / 0.358881**,
and cap 2 **0.368421 / 0.361111 / 0.348649**, respectively. Cap 1 left five cases
short, with six unfilled slots overall. Cap 2 filled all five slots in every
case. This is one additional expected-label occurrence on a development fixture,
not a production-quality improvement or justification to change defaults.

### Portuguese cases: mostly absent candidates

| Mode | Expected occurrences in pool | Baseline hits@5 | Cap 1 / cap 2 hits@5 | Absent from pool |
| --- | --- | --- | --- | --- |
| Semantic | 1 / 10 | 0 / 10 | 1 / 10, both | 9 / 10 |
| Hybrid | 4 / 10 | 1 / 10 | 1 / 10, both | 6 / 10 |
| Lexical | 3 / 10 | 1 / 10 | 1 / 10, both | 7 / 10 |

Both caps recovered `gpc/watcher.py` for case 5 (one-based). Its other expected
file, `docs/watcher.md`, was absent from that semantic pool. Limits per file
cannot recover expected paths absent from the captured candidates. For hybrid,
three Portuguese expected occurrences were present but outside top-five, yet
neither cap recovered them. Presence outside top-five is not itself evidence of
duplicate-file crowding.

### Lexical ties and reproducibility

Every new-fixture lexical channel had a **15-way score tie**, both in lexical
and hybrid modes. The old fixture had three 15-way ties and one 14-way tie plus
a singleton. The current literal-match score adds the same bonus for any
matching term, and ties use chunk IDs. This merits candidate-scoring analysis
before learned reranking.

Some lexical/hybrid metrics differed from the previous independently rebuilt
index despite the same source corpus: old hybrid hits changed 1→2 and new hybrid
4→6. Newly generated chunk IDs can affect tie order. These are **not algorithmic
gains**; traced/untraced and cap comparisons here use the same index and captured
candidate pools. Semantic baseline metrics matched the preceding run.

### Provenance and operational invariants

- Indexed source: `102cabf4f417984516add2e4921285ce46f8b83f`; all 107 source blobs
  matched the fixed manifest. Application execution used the current checkout,
  with 41 Python-module hashes separately verified before/after.
- Canonical application inventory SHA-256:
  `7dd57bfdaa4eed2cb4798091cdc2726dc5a3a27f86d5b217cc098cda6ae6166c`.
- Frozen capture-bundle SHA-256:
  `8ebd1494b2a1b1a2a0428e130a2a337ae3ae0f766f804aff8e8e9f9aff29baf3`.
- Production's 19 table counts, repository registrations, migration checksums,
  478 vector points and container/image/mount identities were unchanged.
- Both owned resources were removed and independently verified absent. Failed
  harness attempts remain preserved; neither performed indexing or evaluation.
- Entity extraction and self-metrics were externally suppressed as in the prior
  experiment; Neo4j acquisition was forbidden. Native indexing and search were
  otherwise unchanged. Existing local embeddings were used; no downloads.
- Protected captures and exact harnesses remain local. Future replay can reuse
  them without another index or model call. Fixed-order and guard-instrumented
  timings are not performance benchmarks.

## Decision

Keep the current ranking defaults and the caps diagnostic-only. Candidate
generation, especially lexical score discrimination and the missing expected
paths in Portuguese cases, is the next priority. A follow-up should predeclare
its scoring policy and evaluate independently judged or paired-language cases,
not tune against these twelve development judgments. AST retrieval and learned
reranking remain unimplemented; the present evidence does not yet justify them
as the next fix.
