# Graphify and Neo4j

GPC answers semantic questions through MCP, Postgres, Qdrant and Ollama.
[Graphify](https://github.com/rmarquesa/Graphify) complements that surface
with a structural relationship graph extracted directly from each repository.

This document is the canonical reference for wiring Graphify into GPC's Neo4j
service.

## What Graphify Adds

GPC's chunk + vector retrieval answers *"find me the code that talks about
X"*. Graphify answers structural questions:

- which worker calls which service;
- which module touches which database table;
- which projects share an integration point;
- which files sit between an API route and the persistence layer.

Each Graphify run produces three artifacts under `graphify-out/`:

| File | Purpose |
|---|---|
| `graph.json` | Machine-readable graph data (nodes and edges). |
| `graph.html` | Interactive graph visualization. |
| `GRAPH_REPORT.md` | Human-readable audit (god nodes, communities, suggestions). |

GPC consumes `graph.json` and projects it into Neo4j.

## Consolidation Model

The GPC docker compose ships with **Neo4j Community**, which supports a
single user database. GPC works around this by namespacing nodes with
`project_slug` and `repo_slug` rather than relying on multi-database
separation.

```text
(:GraphifyProject {slug: "commerce"})
  -[:HAS_REPO]-> (:GraphifyRepo {slug: "commerce-api"})
                   -[:HAS_GRAPH_NODE]-> (:GraphifyNode { … })
                                          -[:GRAPHIFY_RELATION]-> (:GraphifyNode { … })
```

Node identifiers are stable across reruns:

```text
<project_slug>:<repo_slug>:<graphify_node_id>
```

This lets one logical GPC project (e.g. `commerce`) consolidate Graphify data
from multiple Git repositories (`commerce-api`, `commerce-workers`,
`commerce-web`) into a single navigable graph.

## Per-Repository Setup

Each repository that should contribute to the consolidated graph needs three
things: GPC initialization, a Graphify run, and a hook that pushes the result
into Neo4j after each commit.

### 1. Initialize GPC and Graphify

```bash
cd /path/to/commerce-api
gpc init . --slug commerce-api --name "Commerce API"
graphify update .
```

### 2. Configure the Graphify environment

Create `.gpc/graphify.env` (the `.gpc/` directory is gitignored, so secrets
do not leak):

```bash
mkdir -p .gpc
cat > .gpc/graphify.env <<'EOF'
GPC_GRAPHIFY_ENABLED=1
GPC_GRAPHIFY_PROJECT_SLUG=commerce
GPC_GRAPHIFY_REPO_SLUG=commerce-api
GPC_GRAPHIFY_LOG=.gpc/graphify-neo4j.log
GPC_GRAPHIFY_GRAPH_PATH=graphify-out/graph.json
GPC_GRAPHIFY_NEO4J_URI=bolt://localhost:7687
GPC_GRAPHIFY_NEO4J_USER=neo4j
GPC_GRAPHIFY_NEO4J_PASSWORD=gpcneo4jpass
GPC_GRAPHIFY_NEO4J_PYTHON=/absolute/path/to/GPC/venv/bin/python
GPC_GRAPHIFY_NEO4J_CLEAR_REPO=1
EOF
```

### 3. Wire the post-commit hook

```bash
cat > .git/hooks/post-commit <<'EOF'
#!/usr/bin/env bash
set -a
[ -f .gpc/graphify.env ] && . .gpc/graphify.env
set +a
/absolute/path/to/GPC/examples/hooks/graphify-neo4j-post-commit.sh
EOF
chmod +x .git/hooks/post-commit
```

Wire the same wrapper from `post-merge` or `post-checkout` if you also want
Graphify to refresh after branch changes.

### 4. Add another repository to the same project

For each additional repo, keep `GPC_GRAPHIFY_PROJECT_SLUG` constant and only
change `GPC_GRAPHIFY_REPO_SLUG`:

```env
GPC_GRAPHIFY_PROJECT_SLUG=commerce
GPC_GRAPHIFY_REPO_SLUG=commerce-workers
```

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `GPC_GRAPHIFY_ENABLED` | yes | Set to `1` to allow the hook to run. |
| `GPC_GRAPHIFY_PROJECT_SLUG` | yes | Logical GPC project name. |
| `GPC_GRAPHIFY_REPO_SLUG` | yes | Repository identifier inside the project. |
| `GPC_GRAPHIFY_GRAPH_PATH` | yes | Path to the Graphify output JSON. |
| `GPC_GRAPHIFY_LOG` | no | Hook log file. Defaults to `.gpc/graphify-neo4j.log`. |
| `GPC_GRAPHIFY_NEO4J_URI` | yes | Neo4j Bolt URI. |
| `GPC_GRAPHIFY_NEO4J_USER` | yes | Neo4j user. |
| `GPC_GRAPHIFY_NEO4J_PASSWORD` | yes | Neo4j password. |
| `GPC_GRAPHIFY_NEO4J_PYTHON` | yes | Python interpreter with the `neo4j` driver installed (the GPC venv works). |
| `GPC_GRAPHIFY_NEO4J_CLEAR_REPO` | no | When `1`, clears only the current `project_slug + repo_slug` subgraph before writing. Other repos in the same project are untouched. |

## Query Examples

All repositories registered under one project:

```cypher
MATCH (project:GraphifyProject {slug: "commerce"})-[:HAS_REPO]->(repo)
RETURN project.slug, collect(repo.slug) AS repos;
```

Nodes inside one repo:

```cypher
MATCH (project:GraphifyProject {slug: "commerce"})-[:HAS_REPO]->(repo)-[:HAS_GRAPH_NODE]->(node)
WHERE repo.slug = "commerce-api"
RETURN node.label, node.file_type
LIMIT 25;
```

Relationships inside one repo:

```cypher
MATCH (a:GraphifyNode {project_slug: "commerce", repo_slug: "commerce-api"})-[r:GRAPHIFY_RELATION]->(b)
RETURN a.label, r.relation, b.label
LIMIT 25;
```

## Operational Notes

- The Graphify hook is independent from GPC's auto-index hooks. They write to
  different stores: GPC indexer → Postgres + Qdrant; Graphify hook → Neo4j.
- The MCP server stays read-only. It does not push Graphify data into Neo4j.
- The example hook runs in the background and logs to
  `.gpc/graphify-neo4j.log`. Inspect it if a commit completes but the graph
  does not refresh.
- Keep credentials out of Git. `.gpc/graphify.env` is covered by the GPC
  init's `.gitignore` entry.
- If you reset the Neo4j volume, rerun `graphify update .` followed by a
  commit (or invoke `examples/hooks/graphify-neo4j-post-commit.sh` directly)
  to repopulate.

## See Also

- [Architecture](architecture.md#neo4j--graph-projections-optional) — how the
  Graphify subgraph coexists with GPC's own Neo4j projection.
- [Automation](automation.md) — GPC's own auto-index hooks.
