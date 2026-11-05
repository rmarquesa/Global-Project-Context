# Paired retrieval fixture

This development fixture is fixed at `tests/fixtures/search_eval_paired.yml`.
It contains eight authored questions, each ordered English then Portuguese,
with identical repository, task, and minimal relevant-file labels per pair.
Pairs 1–4 use ordinary phrasing; pairs 5–8 intentionally include concrete
identifiers, paths, or literals in both languages. It is a small development
fixture, not representative coverage, a held-out set, or human-approved gold.

## Frozen source and provenance

Labels were authored against the materialized corpus for source revision
`102cabf4f417984516add2e4921285ce46f8b83f`, frozen manifest SHA-256
`97f44a5c479e5caa053e7da1229ec0f055c7071c9d94dd6da17425d33ade17b7`.
That manifest reports 107 included files and 104 indexed. The corresponding
materialized files, not the edited working tree, informed these labels.
Fixture and provenance documentation are new evaluation artifacts and were
excluded from that frozen corpus; do not add either to the corpus when
measuring. No weighted retrieval implementation or evaluation result informed
query or label selection.

| Pair (EN/PT index) | Task | Expected paths | Source anchor and judgment |
|---|---|---|---|
| 1 (1/2) | debug | `gpc:scripts/migrate.py` | `check_for_checksum_mismatches` (frozen `scripts/migrate.py:63`): identifies edited already-applied migrations. |
| 2 (3/4) | implement | `gpc:scripts/install_mcp_clients.py` | `install_client` (frozen `scripts/install_mcp_clients.py:129`) dispatches client-specific installers; `install_codex` (line 167) selects `~/.codex/config.toml` (line 175). The file is the minimal source for which config files are targeted. |
| 3 (5/6) | review | `gpc:gpc/token_economy.py` | `indexed_token_stats` and `count_tokens` (frozen `gpc/token_economy.py:118,140`): computes token accounting from indexed data. |
| 4 (7/8) | review | `gpc:gpc/mcp_observability.py` | `redact_sensitive` (frozen `gpc/mcp_observability.py:62`): removes sensitive values from logged call data. |
| 5 (9/10) | debug | `gpc:scripts/migrate.py` | `check_for_checksum_mismatches` (`scripts/migrate.py:63–76`) detects checksum mismatches for already-applied migrations. The SQL file defines a migration but does not detect the mismatch, so it was removed from both labels before measurement. |
| 6 (11/12) | review | `gpc:gpc/mcp_observability.py` | `redact_sensitive` (`gpc/mcp_observability.py:62`): literal symbol/path plus benign password-redaction concern. |
| 7 (13/14) | debug | `gpc:gpc/registry.py` | `resolve_repo` (`gpc/registry.py:472`): explicit repository-resolution behavior. |
| 8 (15/16) | review | `gpc:gpc/token_economy.py` | `count_tokens` (`gpc/token_economy.py:140`): literal symbol/path and its text-token estimate. |

These are developer-authored source judgments with agent review, not human
approval. There are five distinct expected source files, with topics reused
across natural and identifier-bearing pairs; this is not eight independent
domains. Labels deliberately name 1–2 files rather than broad roadmap topics.
English and Portuguese wording expresses the same single retrieval intent in
each pair; semantic equivalence remains a review judgment, not a unit-test
claim. The standalone test checks structure, pair order/labels, safe existing
paths, parser parity, and query limits without services or embeddings.

Fixture SHA-256 at corrected pre-measurement freeze: `ec04a57fcd97954f6db6ac299bebbbd23ce3ce69e66ef76515e18d5ee8be1069`.
