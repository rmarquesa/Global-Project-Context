# GPC Landing Page

Static English product site for GPC. Preserve its warm-paper/dark-graph visual
identity, native HTML interactions and dependency-free implementation.

## Feature maintenance checklist

Every feature delivery must update **both the documentation and this website**.

- Check implemented CLI/MCP arguments against help, code and tests; keep examples
  copyable and distinguish sample placeholders from real IDs.
- Link to current docs: [watcher](../docs/watcher.md),
  [hybrid retrieval](../docs/hybrid-search.md),
  [decision memory](../docs/decision-memory.md),
  [context-engine evolution](../docs/plans/2026-11-04-gpc-context-engine-evolution.md)
  and [roadmap](../docs/roadmap.md).
- Keep hero/value, pipeline, context-engine, roadmap and FAQ claims consistent.
  Distinguish **implemented**, **live validation pending** and **planned**. Do not
  imply automatic migrations, authenticated actor labels, autonomous chat memory,
  live-Git evidence, production-quality gains or unmeasured latency improvements.
- The MCP demo replays `data/demo-queries.json`; it does not call a backend. Its
  historical token counts are not hybrid-search or answer-quality benchmarks.
  Keep any new illustrations clearly labeled and preserve keyboard interactions.
- Inspect at 1280px and 390px. Check headings/anchor IDs, command wrapping, page
  overflow, visible keyboard focus, demo presets, graph controls and browser errors.
- Run `node --check site/mcp-demo.js` and `git diff --check`. Do not rewrite graph
  assets to accompany unrelated copy changes; coordinate graph updates separately.

## Local preview

From the repository root (serve HTTP so bundled data/modules load correctly):

```bash
python3 -m http.server 8766 --bind 127.0.0.1 --directory site
# Open http://127.0.0.1:8766, then stop with Ctrl+C.
```

No backend, migration or deployment is needed to preview. Publish `site/*` using
the existing static-hosting workflow only when explicitly authorized.
