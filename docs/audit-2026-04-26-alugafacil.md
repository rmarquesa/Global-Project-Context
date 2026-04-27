# AlugaFacil GPC Audit — 2026-04-26

This audit uses `alugafacil` as the real multi-repo validation target for GPC.
It covers Postgres/Qdrant indexing, Neo4j Graphify projection, MCP retrieval,
token-economy observability and drift detection.

## Executive Summary

`alugafacil` is correctly modeled as one logical GPC project owning 21 local
repositories. Postgres and Qdrant are aligned at 1,264 indexed files, 2,532
chunks and 2,532 vector points. The Neo4j projection is active, with 2,877
Graphify nodes and 11,765 cross-repo bridge edges.

The highest-signal remaining concern is graph precision: 11,765 inferred
cross-repo bridges vs. 4,142 extracted same-repo edges means structural
answers are useful, but callers should keep the default
`min_confidence="EXTRACTED"` unless they explicitly want inferred bridges.

## Project Shape

| Layer | Count |
|---|---:|
| Repositories | 21 |
| Indexed files | 1,264 |
| Chunks | 2,532 |
| Qdrant points | 2,532 |
| Indexed tokens | 596,096 |
| GPC file entities | 1,277 |
| GPC import relations | 978 |
| Graphify repos | 21 |
| Graphify nodes | 2,877 |
| Graphify same-repo edges | 4,142 |
| Cross-repo bridges | 11,765 |
| Weakly connected Graphify nodes | 232 |
| Graph communities | 243 |

## Repository Coverage

| Repo | Files | Chunks |
|---|---:|---:|
| admin | 58 | 90 |
| app | 67 | 75 |
| console | 134 | 384 |
| database | 68 | 99 |
| e2e-tests | 23 | 39 |
| web | 423 | 902 |
| workers-chat-ai | 87 | 175 |
| workers-email-consumer | 36 | 55 |
| workers-gateway | 66 | 179 |
| workers-listing-copy-ai | 17 | 22 |
| workers-messaging | 28 | 51 |
| workers-payments | 27 | 38 |
| workers-photos | 21 | 41 |
| workers-photos-moderator | 16 | 25 |
| workers-photos-processor | 14 | 21 |
| workers-properties | 34 | 52 |
| workers-properties-consumer | 24 | 48 |
| workers-properties-moderator | 18 | 30 |
| workers-users | 47 | 109 |
| workers-visit-reports | 13 | 20 |
| workers-visits | 43 | 77 |

## MCP Retrieval Checks

Representative semantic query:

```text
como as fotos de imóveis são armazenadas e publicadas?
```

Top repositories returned by `gpc.search`:

1. `workers-photos`
2. `workers-photos`
3. `workers-photos-moderator`
4. `workers-photos`
5. `workers-photos-processor`

This is the expected retrieval shape for the photo workflow.

Hybrid context query:

```text
como o gateway se relaciona com workers de fotos?
```

`gpc.context(include_graph=true, graph_min_confidence="INFERRED")` returned a
3,498-character context block with sources from:

- `workers-gateway`
- `admin`
- `e2e-tests`

This confirms graph-augmented context degrades into the surrounding API and
test surfaces instead of only returning the photos worker.

## Token Economy

For the photo workflow query, `gpc.estimate_token_savings` reported:

| Metric | Value |
|---|---:|
| Indexed tokens | 596,096 |
| Retrieved tokens | 1,688 |
| Saved tokens | 594,408 |
| Savings percent | 99.72% |

Persisted token-economy samples for `alugafacil`:

| Metric | Value |
|---|---:|
| Samples | 34 |
| Total saved tokens | 20,017,277 |
| Average savings percent | 99.52% |

Grafana dashboard: `GPC / GPC Token Economy`, available locally at
`http://localhost:3300` in this environment.

## Graph Summary

Top central nodes from `gpc.graph_summary`:

| Label | Repo | Degree |
|---|---|---:|
| `resolveSecret()` | workers-payments | 95 |
| `resolveSecret()` | workers-email-consumer | 94 |
| `resolveSecret()` | workers-listing-copy-ai | 94 |
| `extractVerifiedUserContext()` | workers-properties | 92 |
| `resolveSecret()` | workers-properties-consumer | 91 |
| `resolveSecret()` | workers-photos-moderator | 91 |
| `resolveSecret()` | workers-properties-moderator | 91 |
| `extractVerifiedUserContext()` | workers-visits | 90 |

Cross-repo bridge mix:

| Rule | Confidence | Count |
|---|---|---:|
| same_source_file | INFERRED | 9,895 |
| same_code_symbol | INFERRED | 1,616 |
| content_hash | INFERRED | 254 |

The graph correctly exposes duplicated Cloudflare worker utility surfaces
(`auth.js`, `crypto.js`) as cross-repo hubs. That is useful for audit and
refactor discovery, but it is still inferred evidence.

## Path Check

`gpc.graph_path` found a short inferred path between shared auth/crypto
symbols:

```text
extractVerifiedUserContext()
  -> resolveSecret() via CROSS_REPO_BRIDGE same_source_file src/utils/auth.js
  -> resolveSecret() via CROSS_REPO_BRIDGE same_code_symbol resolveSecret()
```

This validates cross-worker traversal, with confidence carried on every hop.

## Drift

`gpc metrics drift --project alugafacil --window-hours 24` currently emits no
signals. That is expected: recent snapshots are stable enough against the
configured rule thresholds.

Current drift rules are intentionally conservative:

- INFERRED graph share rises by more than 10 percentage points.
- AMBIGUOUS graph share rises by more than 5 percentage points.
- Weakly connected nodes spike by at least 50% and 10 nodes.
- Community count spikes by at least 50% and 3 communities.
- Top graph hubs change.

## Follow-ups

1. Add a `package_import` bridge rule so shared package imports can upgrade
   selected relationships from inferred to extracted.
2. Add a weekly cron for `gpc metrics collect --project alugafacil` and
   `gpc metrics drift --project alugafacil`.
3. Keep Grafana retention enabled with:

   ```bash
   gpc maintenance retention --mcp-days 30 --token-days 90
   ```

4. For architectural answers, keep `gpc.graph_*` defaults on
   `min_confidence="EXTRACTED"` and opt into `INFERRED` only when reviewing
   duplicated worker utility code or cross-repo bridges.
