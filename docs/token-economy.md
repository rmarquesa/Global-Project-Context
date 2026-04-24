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

`gpc token-savings` measures **prompt context tokens avoided for one query**,
using an **optimistic baseline**: it assumes the alternative is to send the
entire indexed corpus to the model. In practice no one does that — an agent
without GPC would run `grep` + `Read` on a handful of files instead. The
realistic saving is lower than the headline percentage, but still meaningful.

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
- Free — using the MCP still costs tokens. The tool-use invocation, the
  protocol overhead and the returned context all count against the model's
  prompt budget. The next section quantifies this.

## Realistic Cost Comparison

Headline savings compare `indexed_tokens` (everything ever indexed) against
`retrieved_tokens` (the bounded block for one query). The realistic baseline
is somewhere in between — closer to what an agent without GPC actually spends
on discovery.

### Cost of using the MCP (per query)

| Component | Approx tokens counted against the model |
|---|---:|
| `query` text sent via tool_use | 10–50 |
| MCP tool-use / tool-result JSON overhead | 100–200 |
| Context block returned by `gpc.context` | up to `max_chars / 4` (≈1,500–2,000 with defaults) |
| Query embedding (Ollama, local) | **0** |
| Postgres / Qdrant access | **0** |

Total: ~**1,900–2,200 tokens** for a typical `gpc.context` call.

Structural queries (`gpc.graph_neighbors`, `gpc.graph_summary`,
`gpc.graph_path`) return smaller payloads — typically 500–3,000 tokens —
because they emit nodes and edges, not full text chunks.

### Cost of the "no-MCP" path

Without the MCP, a coding agent discovers context incrementally:

- A `grep` invocation: ~200 tokens for the command + 500–2,000 tokens of
  output, depending on how many matches and how chatty the hits are.
- A `Read` on a source file: 800–3,000 tokens for a typical source file,
  higher for large docs or generated code.
- Extra tries when the first grep/Read was off target.

A single question typically costs **3,000–15,000 tokens** of discovery
before the agent even starts answering.

### Measured examples

The table below shows actual measurements from a multi-repo Cloudflare
workers project indexed in GPC. Corpus: 567 files, 1,240 chunks,
~303,000 indexed tokens.

| Question | Corpus (indexed) | Retrieved via MCP | Headline savings | Realistic comparison |
|---|---:|---:|---:|---|
| "How does the HMAC signature between gateway and users workers work?" | 303,235 | 1,824 | 99.4% | vs. ~5–10k for grep + 2–3 Read calls → **~60–80% saving** |
| "How are favourite properties persisted in the frontend?" | 303,235 | 1,684 | 99.4% | vs. ~5–12k for component discovery → **~65–85% saving** |
| "Who calls `resolveSecret()`?" (structural) | 303,235 | ~500 (`graph_neighbors`) | — | vs. grep + Read across repos (5–20k) → **~90–97% saving** |
| "Give me a map of this project" | 303,235 | ~3,000 (`graph_summary`) | — | vs. reading 8–10 entry-point files (12–25k) → **~75–85% saving** |

Two patterns are visible:

1. **Structural queries win by a wider margin** than semantic retrieval. The
   graph tools answer "who uses X?" in one call; without them the agent
   fans out reads and often lands on the wrong files first.
2. **The cost *per query* with MCP is stable** (~1.5k–3k tokens). The cost
   without it is variable (3k–20k+). Even if the average saving is
   60–85% instead of 99%, the **variance** and therefore the **cache-hit
   rate on the model side** improves dramatically — prompts stay the same
   shape between turns so the Anthropic prompt cache keeps warm.

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
