# Token Economy

GPC does not make model inference free. Its practical gain is reducing how
much project context you repeatedly send to AI tools.

Without GPC the workflow tends to be:

```text
paste files + explain architecture + ask question
```

With GPC the workflow becomes:

```text
ask question → retrieve relevant chunks → send compact context
```

This document explains how `gpc token-savings` measures that reduction, what
the numbers mean, and what they do not include.

## Measuring Savings

Run from any indexed project:

```bash
gpc token-savings \
  "how do I configure GPC for Claude, Codex and Copilot?" \
  --project gpc
```

The command compares two values for that single query:

| Field | Meaning |
|---|---|
| `indexed_tokens` | Sum of token counts across every chunk currently indexed for the project. |
| `retrieved_tokens` | Token count of the bounded context block returned for the query. |
| `saved_tokens` | `indexed_tokens − retrieved_tokens`. |
| `savings_percent` | `saved_tokens / indexed_tokens × 100`. |

`max_chunks` and `max_chars` (passed through to `gpc.context`) cap how big
the retrieved block can grow.

## Worked Example

Measured on this repository after indexing the documentation update:

```text
project=gpc
files=60
chunks=120
indexed_tokens=25737
retrieved_tokens=1387
saved_tokens=24350
savings=94.61%
```

Reading: instead of sending 25,737 tokens of indexed corpus to the model,
the AI client received the 1,387 tokens that actually matched the query.

## What the Number Does and Does Not Mean

`gpc token-savings` measures **prompt context tokens avoided for one query**.
That is genuinely the most expensive recurring cost in agentic workflows
where the same project is referenced over and over.

It is **not**:

- A guarantee — savings depend on project size, indexed coverage, the query
  itself, and the configured retrieval limits.
- A cumulative metric — each query is measured in isolation. Re-ask the same
  question and you "save" the same number again, because in the alternative
  world you would have re-pasted the same context.
- A reduction in model output tokens.
- A reduction in tokens spent on reasoning *after* context is retrieved.
- A measure of accuracy. A query that retrieves nothing relevant will still
  show a high savings percent — it just sent very little because nothing
  matched. Combine with `gpc-search` to confirm the chunks are useful.

## Interpreting Edge Cases

### `retrieved_tokens` is zero

The query returned no chunks above the relevance threshold. Possible causes:

- The project is not indexed yet. Check with `gpc-status --project <slug>`.
- The query is phrased very differently from the indexed text. Try a
  rephrased query or use `gpc-search` to inspect raw matches.
- The relevant content is filtered out by the indexer's ignore rules
  (binaries, generated folders, secret-like content).

### `savings_percent` is close to zero

Either the project is small (so there was not much to save against) or the
retrieval pulled most of the corpus because every chunk matched. Lower
`max_chunks` / `max_chars` if you want a tighter bound.

### `indexed_tokens` looks too low

Run `gpc-status --project <slug>` and confirm the file/chunk counts match
expectations. If files appear missing, rerun `gpc-index` without
`--limit-files` to do a full pass.

## See Also

- [Operations](operations.md#index-a-project) — how to index and inspect a
  project.
- [MCP clients](mcp-clients.md#mcp-tools) — `gpc.estimate_token_savings`
  surfaces the same metric to AI clients.
