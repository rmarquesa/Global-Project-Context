# Diversity retrieval fixture

This developer-authored set contains six English/Portuguese query pairs (12
cases) for examining retrieval breadth. These cases are not held out,
human-approved, or claimed representative. Review the source anchors and labels
before any measurement; do not tune retrieval against this fixture.

The frozen corpus is revision `102cabf` (manifest SHA-256
`97f44a5c479e5caa053e7da1229ec0f055c7071c9d94dd6da17425d33ade17b7`). Newly
added files absent from that corpus are not expected answers. Labels below
include only the most direct sources, not every file mentioning the subject.

| Cases | Topic / source anchor | Why these labels |
|---|---|---|
| 1–2 | Embedding provider/model and vector dimension: `gpc/embeddings.py`, `embed_texts` (83–100), `active_embedding_model` (110–114), `embedding_dimension` (103–107); `gpc/config.py` embedding settings (16–23). | Provider dispatch and model selection are separate: `embed_texts` dispatches by provider, `active_embedding_model` selects the model, and dimension uses configured size or a probe. |
| 3–4 | Cross-store project rename: `gpc/project_rename.py`, `rename_project` (53–99), module contract (1–18). | This operation updates project identity across Postgres, Qdrant, and Neo4j. |
| 5–6 | Graphify confidence comparison: `gpc/graph_query.py`, `_confidence_shift` (567–599). | The helper calculates before/after confidence percentages and percentage-point deltas. |
| 7–8 | Retention deletion-count preview: `gpc/cli.py`, `cmd_maintenance_retention` (1933–1949), `--dry-run` (1939), `would_delete` (1944); `docs/operations.md`, retention `--dry-run` (250–260). | The question specifically concerns previewing deletion counts without deleting usage records, not the diagnostic doctor command. |
| 9–10 | Explicit identifier dimension question: `gpc/embeddings.py`, `embedding_dimension` (103–108). | Both language versions name the same function and file. |
| 11–12 | Explicit helper/snapshot question: `gpc/graph_query.py`, `_confidence_shift` (567–599). | Both language versions ask about the same helper. |

Pair map: 1–2 provider/dimension (`debug`, `gpc/embeddings.py` +
`gpc/config.py`); 3–4 rename (`review`, `gpc/project_rename.py`); 5–6
confidence comparison (`debug`, `gpc/graph_query.py`); 7–8 retention preview
(`review`, `gpc/cli.py` + `docs/operations.md`); 9–10 dimension (`debug`,
`gpc/embeddings.py`); 11–12 helper summary (`debug`, `gpc/graph_query.py`).
The last two pairs intentionally share an explicit identifier/path across
translations; the first four use natural-language wording.

The fixture is excluded from the frozen search index corpus. The parent
reviewer verified that all six distinct expected file paths are both included
and candidate-indexable in the frozen manifest. There are 16 expected-path
label occurrences across the cases (some paths intentionally recur). This is
source-review evidence, not human approval or a retrieval result. Fixture
SHA-256: `8c96fb6e641db0d2bc519a93ae354eab85151514ce1a0ee8695c4a2c6382e7b6`. Freeze this exact content before measurement;
no result is implied by authoring or testing these cases.
