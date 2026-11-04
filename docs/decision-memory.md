# Evidence-backed decision memory

Decision memory preserves reviewed engineering guidance separately from source
chunks and chat history. It is a local, explicit workflow: a person captures a
proposal, reviews its evidence, and approves or invalidates it. GPC does not
automatically turn conversations into trusted rules.

## Prerequisites and scope

Review and apply migrations through the normal `gpc migrate` workflow, including
`0011_decision_memory.sql`. This change does not apply migrations automatically.
Existing decision rows are not automatically approved by the new workflow.

Writes require an explicit project, a registered repository, and an indexed
source path in that repository. GPC records the indexed file's content hash as
evidence. Identical paths in two repositories are separate scopes. No arbitrary
file is opened or ingested when a decision is captured.

## Capture, review, approve

Suppose `src/auth.py` is already indexed in the `api` repository of `product`:

```bash
gpc decision capture --project product --repo api \
  --key auth-policy --title 'Validate sessions centrally' \
  --content 'Session checks belong in the API boundary.' \
  --source-path src/auth.py --actor maintainer

# Use the ID returned by capture; inspect the proposal and evidence first.
gpc decision show <id> --project product --repo api
gpc decision approve <id> --project product --repo api --actor maintainer
gpc decision list --project product --repo api
```

Capture creates a **pending** proposal, not trusted guidance. Approval requires
the evidence to still match the current indexed source. The `--actor` value is
a local attribution label, not an authenticated identity or proof of a review.
Human approval is not a correctness guarantee; inspect the cited source.

An optional `--source-revision` records a caller-supplied revision label. It is
not verified against Git and must not be treated as a verified commit. The
indexed source hash is the actual evidence fingerprint used for freshness.

## Invalidate or supersede

```bash
gpc decision invalidate <id> --project product --repo api \
  --actor maintainer --reason 'The authentication contract has changed.'

# Capture a replacement with the same repo, source path and key, then:
gpc decision approve <new-id> --project product --repo api \
  --actor maintainer --supersede <old-id>
```

Only one decision can be approved for the same project/repository/path/key.
A competing approval fails unless the existing decision is explicitly
superseded. Replacement approval and supersession are one transaction, with
transition history for both decisions. Unrelated repositories or keys cannot be
superseded by accident.

Invalidated and superseded decisions are historical evidence, not current
instructions. To revise guidance, capture another proposal instead of rewriting
approved content. Conflicts are detected by the explicit decision key and scope;
there is no automatic semantic contradiction detector.
Repeated approval/invalidation and attempts to approve a terminal decision return
errors; they are not silently treated as successful new reviews.

## Retrieval and freshness

```bash
# Default: approved guidance whose evidence still matches the index.
gpc decision list --project product --repo api --query session

# Diagnostic view: also inspect proposals, obsolete or invalidated guidance.
gpc decision list --project product --repo api --include-inactive
```

The read-only MCP tool `gpc_decisions` supports project/repository/path scoping,
a text query and bounded results. Normal retrieval returns only approved,
current evidence. Diagnostic retrieval identifies status and freshness explicitly;
inactive entries must not be presented as endorsed guidance. Decision content is
retrieved data, not privileged system instructions.

Freshness is **relative to the index**, not to an unindexed disk edit. A changed
indexed hash or missing source makes evidence ineligible for normal retrieval.
Deleting and recreating the source also invalidates its original file identity,
even when the replacement has an identical content hash.
Use indexing or the optional [watcher](watcher.md) to keep that evidence current.
Reading stale evidence does not silently change its stored status or create
audit events. A fresh file hash does not prove the decision remains logically
correct; invalidate guidance when external requirements change.

Memory remains a separate retrieval surface. Existing search/context-pack calls
do not silently acquire decision text, and their budgets and citations retain
their existing contracts.

## Safety and limitations

- Capture, approval, invalidation and supersession are CLI writes; no MCP memory
  write tool is added. Existing operational MCP logging remains separate.
- Inputs are bounded and source paths must be relative, without traversal.
  Recognized secret patterns are rejected, but the scanner cannot prove arbitrary
  text is free of sensitive information. Review summaries before storing them.
- Evidence is scoped to a registered repository and an indexed source path.
  Project-wide instructions without a source, external issue/PR connectors and
  automatic conversation ingestion are not part of this increment.
- Approval uses transactional coordination and a uniqueness constraint. Local
  tests do not establish production PostgreSQL concurrency or performance.
- Audit history is local database history, not a tamper-proof compliance log.
  Database owners and project reset/deletion can remove it.
  Full schema reset explicitly drops the decision-event table before decisions;
  PostgreSQL `DROP TABLE ... CASCADE` alone would remove the referencing constraint,
  not the child table or its history.

See the [evolution plan](plans/2026-11-04-gpc-context-engine-evolution.md) for
validation evidence and remaining work.
