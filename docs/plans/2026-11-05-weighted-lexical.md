# Experimental weighted lexical candidates

Status: implementation, paired-fixture review, unit tests and native isolated
validation complete. Same-index development gains and regressions are recorded
below. Weighted remains experimental; semantic and legacy defaults are unchanged.

## Motivation

The [candidate analysis](2026-11-05-candidate-analysis.md) found 15-way lexical
score ties in all twelve development queries. Most expected Portuguese sources
were absent from candidate pools; offline file caps recovered only one additional
semantic expected-label occurrence. A reranker cannot promote sources absent
from its input. The next bounded experiment therefore changes candidate scoring,
not AST retrieval, a learned reranker or the embedding model.

## Policy fixed before measurement

Introduce `lexical_strategy=weighted` as an explicit experiment in the Python
search API and `eval-search --lexical-strategy weighted`. `legacy` remains the
default, with its existing behavior and report contracts preserved. The semantic
baseline in a three-mode comparison remains untouched.

The experimental policy is intentionally simple and inspectable:

1. Keep the existing query-size/token-count bounds and literal identifier/path
   handling. Deduplicate terms case-insensitively.
2. Remove only a small, fixed set of English/Portuguese function words from
   multi-term queries. Do not remove domain terms or introduce translation,
   synonyms or stemming. A single-token query is retained even if it matches a
   function word, so short identifiers remain searchable. An all-filtered
   multi-term query returns no lexical candidates rather than falling back to a
   broad search.
3. For each distinct remaining term, count only its strongest matched field:
   **path = 6**, otherwise **title = 3**, otherwise **content = 1**. Sum these
   contributions. Repeated occurrences and repeated query tokens do not inflate
   the score. Do not add the legacy full-text rank bonus.
4. Match escaped literal substrings, retaining `%`, `_`, backslashes, Unicode,
   identifiers and paths as data. Do not interpolate user values into SQL.
5. Require a positive score; preserve project/repository ownership joins and the
   bounded candidate limit. Break experimental ties with stable repository/file/
   chunk metadata rather than random chunk UUIDs as the primary key.
6. Keep RRF, task preferences and candidate budgets otherwise unchanged. Preserve
   the stable experimental tie policy after hydration/final ordering too.

These constants are a predeclared hypothesis, not learned or label-tuned weights.
The implementation records the exact function-word set and policy identity.
Experimental reports/captures must identify the strategy; default payloads stay
compatible. This option is not added to MCP or enabled for normal search.

## Paired development cases

A separate owner authors eight English/Portuguese pairs (sixteen queries) against
the frozen `102cabf` corpus, without inspecting experimental scores or adjusting
queries to the new strategy. Each pair must share repository, task and expected
paths. Four pairs use ordinary natural-language phrasing; four carry the same
explicit identifier/path/literal in both languages. Source anchors and judgment
reasons will be reviewed before evaluation.

These are developer-authored development judgments, not human-approved labels,
a held-out test set or a representative bilingual benchmark. Equivalent phrasing
reduces one confound but cannot establish general language quality. Fixtures,
provenance and generated reports remain outside the indexed corpus.

### Reviewed fixture frozen before measurement

The paired fixture is `tests/fixtures/search_eval_paired.yml`, SHA-256
`ec04a57fcd97954f6db6ac299bebbbd23ce3ce69e66ef76515e18d5ee8be1069`.
Its sixteen queries have sixteen expected-path occurrences over five distinct
files. Topics are reused across natural-language and identifier-bearing pairs;
this is not eight independent product domains. The
[provenance guide](../paired-retrieval-fixture.md) lists pair order and anchors.

The coordinator independently checked the frozen migration checksum detector,
client-installer dispatch/configuration paths, indexed token statistics/token
counter, sensitive-key redaction and repository resolution. Before any scores,
the review removed an irrelevant migration-definition label from the checksum
detection pair and corrected the installer anchor. Translations preserve the
same retrieval intent within each pair. Four integrity tests passed, including
the explicitly unavailable-PyYAML path; Ruff and Black passed. This is agent
review of development labels, not human approval.

## Ownership and acceptance

- Implementation owner: lexical SQL/term policy, search ordering, strategy-aware
  evaluation/capture/CLI integration and focused regression tests.
- Fixture owner: only the new paired fixture, provenance guide and pure integrity
  test. No shared-source edits or access to measurement results during authoring.
- Coordinator: source/translation review, documentation/site, full validation and
  reconciliation. SQL ownership and parameterization require focused review
  before live testing.
- Runtime owner, after source/fixture acceptance: one owned isolated index of the
  unchanged corpus, legacy/weighted comparisons on the same index, direct SQL
  policy checks, production invariance and exact cleanup. Reuse transaction-
  neutral ownership guards, manifest-only materialization and module-reload
  rehearsal from the successful prior harness.

Unit acceptance includes default-result/schema compatibility, semantic-baseline
invariance, escaped SQL parameters and unchanged scope checks, term filtering and
deduplication, stable experimental ties, safe capture metadata, unchanged metric
denominators and operation without optional PyYAML. Full-suite checks run with
service endpoints unavailable.

Runtime acceptance measures candidate coverage, recall@5, MRR, nDCG, score ties
and same-index repeatability for both strategies. Report per-language slices and
pair outcomes without tuning the policy after seeing scores. Reject rollout if
results regress or remain inconclusive; preserving the default is intentional.

### Completed source and unit checks

- Full suite with PyYAML: **827 passed**, four service integration tests excluded.
- Full suite with PyYAML import blocked: **821 passed**, six PyYAML-only cases
  skipped, four service integration tests excluded. Both runs used unavailable
  loopback endpoints for backing services.
- Ruff and Black passed seven changed/new Python files; curated Mypy passed five
  modules. CLI help exposes the explicit strategy choice.
- Independent scoped review found no blocking security/correctness issue in the
  weighted CTE, five SQL parameters, unchanged ownership joins, stable metadata
  ordering, or strategy-aware capture/report contracts.
- The arithmetic unit test adapts SQL to SQLite for an independent oracle. That
  is not PostgreSQL integration evidence; native syntax, score arithmetic,
  escaping, ownership, result cap and UUID-independent ordering remain explicit
  runtime acceptance checks.

Weighted policy identity is `weighted-v1`, with 131 fixed function words in
`gpc/retrieval.py`. Reports carry the strategy/policy/tie identity; weighted
captures are version 2 and legacy/semantic captures remain version 1. Internal
hydration adds chunk index/hash for sorting but does not expose either as new
source-content fields in captures. A single weighted semantic request fails
before services, while comparison semantic baselines run unchanged.

### Initial native validation attempt

The first isolated harness executed the native weighted SQL 25 times in
read-only transactions. Arithmetic, literal matching, deduplication, stopword
and ownership assertions passed up to the executed point, but the complete
contract suite did not finish: its metadata collection incorrectly called
`current_setting('lc_collate')`, which PostgreSQL rejected.

No corpus index, embedding or CLI comparison ran. Synthetic rows and both owned
resources were removed; production snapshots matched. The correction reads
database collation metadata from `pg_database.datcollate/datctype` instead.
Earlier evidence remains preserved, and no scoring/fixture changes are permitted
in the continuation. This is a harness correction, not a product SQL change or
evidence of retrieval quality.

## Explicit limits

This is not BM25, a linguistic stemmer, translated-query retrieval or semantic
understanding. Literal substring matches can still be noisy; function-word
filtering can remove useful natural-language terms. Stable ties improve
reproducibility, not relevance by themselves. The new policy may help explicit
identifiers more than natural-language questions. Runtime costs and all observed
regressions must be reported, not hidden by a favorable aggregate.

## Completed native validation

After the catalog-query correction, all nine PostgreSQL contract groups passed:
**25 native weighted queries in 25 read-only/repeatable-read transactions**.
They verified the score oracle, escaping, query-term behavior, ownership joins,
309 matches capped to 300, and stable metadata identities after regenerating
chunk UUIDs. PostgreSQL used `en_US.utf8`: its tie order differs from Python's
Unicode order, but each was independently stable. Cross-platform identical
collation ordering is not claimed.

One corpus index produced **104 files and 320 chunks**, with zero errors and
exact PostgreSQL/Qdrant identifier parity. All expected labels in all three
fixtures were available. Six comparisons produced **18 mode reports, 192 search
calls, 192 validated captures and 384 offline cap replays**. Each strategy's
semantic baseline was identical in paths, IDs, scores, captures, diagnostics and
metrics except measured latency. No semantic improvement is attributed to this
change.

A further sixteen-query paired lexical-only repeat used the same owned index,
with embedding and vector-retrieval functions configured to raise if called.
Neither was called; paths, IDs, scores and capture hashes matched exactly. This
was in-process dependency-failure instrumentation, not a service shutdown or a
fresh-process network-outage test.

### Primary results: legacy → weighted, same index, k=5

| Fixture / mode | Expected-label pool hits | Top-five hits | Recall@5 | MRR@5 | nDCG@5 |
| --- | --- | --- | --- | --- | --- |
| Existing / hybrid | 9→8 /10 | 3→4 /10 | .300→.400 | .383→.583 | .257→.411 |
| Existing / lexical | 3→5 /10 | 3→4 /10 | .300→.400 | .333→.446 | .229→.322 |
| Context / hybrid | 9→15 /19 | 5→9 /19 | .263→.474 | .174→.392 | .187→.368 |
| Context / lexical | 2→13 /19 | 0→8 /19 | .000→.421 | .000→.417 | .000→.364 |
| Paired / hybrid | 15→15 /16 | 9→12 /16 | .563→.750 | .408→.578 | .447→.622 |
| Paired / lexical | 1→12 /16 | 1→12 /16 | .063→.750 | .031→.609 | .039→.645 |

Pool coverage is not recall@5 or a judged relevance ceiling. Candidate budgets
were unchanged: fifteen per active channel, with hybrid using their union.
These comparisons must not be combined with earlier runs' legacy scores, whose
regenerated chunk UUIDs can change tie order.

### Paired slices and regressions

| Slice | Hybrid hits: legacy→weighted | Lexical hits: legacy→weighted |
| --- | --- | --- |
| English | 8→8 /8 | 0→7 /8 |
| Portuguese | 1→4 /8 | 1→5 /8 |
| Natural phrasing | 5→4 /8 | 1→4 /8 |
| Explicit identifiers/paths | 4→8 /8 | 0→8 /8 |
| Natural Portuguese | **1→0 /4** | 1→1 /4 |
| Identifier Portuguese | 0→4 /4 | 0→4 /4 |

Pair-total hit-count wins/losses/equal: hybrid **4/1/3**, lexical **7/0/1**.
The aggregate hybrid gain on Portuguese cases comes from identifier-bearing
queries; natural Portuguese regresses. Identifier gains do not establish better
general natural-language understanding.

Two case/mode combinations lost a hit:

- Existing hybrid case 1, MCP read-only behavior: `docs/architecture.md` left
  top-five; candidate-pool expected hits also fell from three to two.
- Paired hybrid case 4 (pair 2, Portuguese natural phrasing), client installer:
  `scripts/install_mcp_clients.py` remained in the pool but left top-five.

Six more case/mode combinations had lower MRR/nDCG without losing a hit. All eight
metric-regression entries are retained in the protected result artifacts.
Weighted top-five file diversity generally decreased: in the paired fixture,
hybrid distinct-file occurrences per query summed to 69→46 and lexical to 78→35.
Large 15-way ties still occurred. This is not a universal fix for lexical ties
or repeated-file concentration.

### Provenance, safety and limits

- Indexed source remained `102cabf`; application code executed from the current
  checkout and all 41 module hashes were unchanged during runtime.
- Frozen policy SHA-256, including terms/weights/limits/module identity:
  `6bb43c091b083a7d7c0617362b0543a8d1d40410de515e04e7075b0463a381cd`.
- Raw capture-bundle SHA-256:
  `bbf986f53eb8129095171604c01b1182906def844995a74b6270fa8811bbca1a`.
- Production's 19 table counts, registry, migration checksums, 478 vector points
  and container/image/mount identities matched fresh before/after snapshots.
  Both owned resources were removed and independently verified absent.
- Entity extraction/self-metrics were suppressed externally as in earlier
  isolated experiments, and Neo4j access was forbidden. No model download,
  production reindex/rebind/migration or service lifecycle change occurred.
- An offline checker initially compared derived summary JSON by byte ordering;
  it was corrected to compare derived objects while preserving raw-report byte
  checks. Saved results were reused: no reindex or evaluation rerun occurred.
- These are a small fixed development corpus and authored labels. Instrumented,
  fixed-order timings are not a performance/scalability benchmark. The SQL
  output cap does not bound server-side scoring work independently of corpus size.

## Decision after measurement

Publish `weighted-v1` as an explicit experiment only. Keep semantic search and
legacy lexical defaults unchanged. The evidence supports further investigation
of identifier retrieval, but the natural-Portuguese hybrid loss prevents a
general rollout recommendation. Preserve the frozen weights and labels rather
than tune them against this same fixture. Next work should investigate the
recorded lost-hit cases and repeated-file concentration using saved captures,
then validate any candidate policy on separately reviewed cases.
