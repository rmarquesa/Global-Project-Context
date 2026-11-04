# Retrieval evaluation fixture (draft)

`tests/fixtures/search_eval_context_engine.yml` is a small, developer-authored
set of source-grounded candidate judgments for the existing deterministic
retrieval evaluation CLI. It is not an agent benchmark, a human-approved label
set, a representative sample, or a disjoint holdout. No answer-quality or
ranking-quality claim follows from its scores. Labels identify plausible files
to inspect, not necessarily the only relevant evidence; review them before
using results to change ranking behavior.

The [recorded local comparison](plans/2026-11-04-retrieval-diagnostics.md)
exposes both coverage and retrieval misses; it did not demonstrate a hybrid or
lexical improvement. Semantic retrieval remains the default.

## Cases and provenance

The language split is six English and six Portuguese queries. Paths below are
repo-qualified (`gpc:<relative path>`); each case scopes to repo `gpc`.

| # | Topic / language | Query | Expected paths and source anchor | Judgment rationale |
|---|---|---|---|---|
| 1 | Default semantic vs opt-in hybrid / EN | When should search stay semantic, and how does opt-in hybrid fusion combine lexical and vector rankings? | `gpc:gpc/search.py` (`search_project_context`); `gpc:docs/hybrid-search.md` (ranking and modes) | Search dispatch and user-facing mode/fusion contract jointly answer the distinction. |
| 2 | Repository scope and project identity / EN | How do I limit retrieval to the api repository without mixing another project's files? | `gpc:gpc/search.py` (`search_project_context`); `gpc:gpc/registry.py` (`resolve_project`) | Retrieval applies repository scope while registry resolution establishes project identity. |
| 3 | Symlinks and secret exclusion / PT | Como a indexação evita seguir symlinks e incluir arquivos que parecem conter segredos? | `gpc:gpc/indexer.py` (discovery and sensitive-file checks); `gpc:SECURITY.md` (threat-model limits) | Indexer behavior and documented limitations are both needed; detection is heuristic, not a guarantee. |
| 4 | Repair missing vectors / EN | How are missing vectors repaired for unchanged files without re-embedding the entire repository? | `gpc:gpc/indexer.py` (`_index_file`, `_file_point_ids`) | The case asks about repair of absent vectors while preserving unchanged-file work; it does not ask about absent SQL chunks. |
| 5 | Watcher scheduling and branches / PT | Como o watcher combina debounce com um limite máximo e detecta troca de branch? | `gpc:gpc/watcher.py` (`_Schedule`, branch identity); `gpc:docs/watcher.md` (scheduling and branch behavior) | Scheduler bounds coalescing while branch identity drives reconciliation; docs describe observable behavior. |
| 6 | Index lock contention / EN | What prevents two indexing processes from modifying the same repository at once? | `gpc:gpc/index_lock.py` (`repository_lock`) | The repository-scoped lock is the direct coordination mechanism. |
| 7 | Approval and current source hash / PT | Como uma decisão pendente é aprovada somente quando o hash da fonte continua atual? | `gpc:gpc/decisions.py` (`approve_decision`, `_source`); `gpc:docs/decision-memory.md` (review and freshness) | Approval checks current indexed evidence; documentation clarifies proposal versus approval. |
| 8 | Stale decision and deleted source / EN | What happens to an approved decision when its source file is deleted or its evidence becomes stale? | `gpc:gpc/decisions.py` (`_source`, decision listing); `gpc:docs/decision-memory.md` (retrieval and freshness) | Source lookup and stale-evidence behavior determine whether guidance remains endorsed. |
| 9 | Bounded context and citations / PT | Como o context pack mantém o limite de tamanho e inclui citações para os trechos recuperados? | `gpc:gpc/context_pack.py` (`build_context_pack`, `render_context_pack`); `gpc:docs/hybrid-search.md` (context limits) | Pack construction/rendering supplies citations and budget behavior; docs state exposed limits. |
| 10 | Legacy evaluation metrics / EN | Which ranking metrics does the legacy search evaluation calculate, and what do they measure? | `gpc:gpc/search_eval.py` (legacy deterministic metric calculation) | This case asks about existing metric semantics, not the new diagnostics feature or answer quality. |
| 11 | MCP content versus telemetry / PT | Como distinguir ferramentas MCP de leitura de conteúdo das métricas operacionais de uso? | `gpc:gpc/mcp_server.py` (`mcp_search`, `mcp_context`, `mcp_self_metrics`, `mcp_usage`) | The server defines both read-only retrieval tools and operational telemetry endpoints; names/semantics distinguish them. |
| 12 | Project alias resolution / PT | Como o GPC resolve um alias de projeto para a identidade do projeto registrado? | `gpc:gpc/registry.py` (`add_project_alias`, `resolve_project`, `_resolve_by_slug_or_alias`) | Alias lookup is implemented by registry resolution, not by retrieval ranking. |

These judgments were drafted from the listed source functions and current docs;
they have not been independently adjudicated. Some relevant files may be absent
from the currently registered `gpc` corpus, so a miss can reflect corpus
coverage rather than ranking. Expected paths must not be interpreted as
complete relevance judgments.

## Reproduce locally

From the repository root, with the project database and its registered `gpc`
repository available:

```sh
python -m gpc.cli eval-search --project gpc --fixture tests/fixtures/search_eval_context_engine.yml --k 5 --compare-all --diagnose --json
python -m gpc.cli eval-search --project gpc --fixture tests/fixtures/search_eval_context_engine.yml --k 5 --mode lexical --diagnose --json
```

The first command compares the available semantic, hybrid, and lexical modes;
the second is a lexical-only offline-embedding run. `--compare` remains the
existing semantic-versus-hybrid comparison. The commands query the registered
corpus; do not automatically reindex or rebind the project to make a fixture
path appear.

Diagnostics classify expected paths by indexed-chunk availability, not vector
presence or current live-file freshness. They are SQL availability checks;
they do not prove that a vector exists or that the indexed content matches the
working tree. A project/repo read failure or unknown scope must not be treated
as a valid zero-coverage comparison. Record the repository code Git SHA
alongside results externally: fixture/metadata fingerprints are not a verified
Git commit. Concurrent index changes can invalidate a comparison, and fixed
mode order/cache effects can bias timing, so these commands are not a ranking
benchmark. Do not commit result JSON containing indexed source excerpts.

## Read the diagnostic report

`--diagnose` adds `diagnostics` to each report and each case. Default evaluation
output is unchanged when the flag is omitted. Existing recall, MRR and nDCG
denominators are unchanged even when expected sources are absent.

| Availability status | Meaning |
| --- | --- |
| `indexed` | At least one matching file has retrievable project-file chunks in PostgreSQL. |
| `file_without_chunks` | A matching file exists, but has no eligible chunks. |
| `not_indexed` | No matching file was found in the selected scope. |
| `ambiguous` | An unqualified path matches more than one repository; qualify the judgment. |
| `out_of_scope` | A qualified expected repository is excluded by the case's repository filter. |

Coverage totals count distinct labels **within each case**, then sum across cases.
They are not counts of unique files across the fixture. `indexed_but_not_retrieved_paths`
identifies chunk-backed labels missed at k; missing vectors or retrieval behavior
may still explain the miss. `not_indexed_expected_paths` includes both absent
files and files without chunks. Unknown requested repositories fail explicitly
in diagnostic mode rather than becoming apparently successful zero-coverage runs.

`diagnostics.snapshot` identifies the resolved project/repositories and includes
`fixture_sha256` plus `metadata_sha256`. The latter covers matching fixture-path
metadata only, **not the whole searchable corpus**, embedding model state or Git
revision. The snapshot omits file contents and absolute repository roots.

`--compare-all` retains `baseline` (semantic), `candidate` (hybrid) and `delta`
(hybrid minus semantic), adding `lexical` and `lexical_delta` (lexical minus
semantic). Modes execute in semantic → hybrid → lexical order. All use one
parsed fixture and, with diagnostics enabled, the same metadata snapshot.

Before optimizing ranking, have a reviewer inspect every expected path and
rationale, then add held-out user queries with independently reviewed
relevance judgments. In particular, no-answer correctness is not scored here:
an expected-path hit or retrieval metric cannot establish that a generated
answer should have abstained, nor that any answer is correct.
