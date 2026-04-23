#!/usr/bin/env bash
# Example Graphify -> Neo4j hook for a project that also uses GPC.
#
# Use it from a project's .git/hooks/post-commit, post-merge, or post-checkout.
# It rebuilds graphify-out/graph.json with graphify's code-only updater and then
# pushes that graph into Neo4j.
#
# This project uses neo4j:5-community, so all project graphs live in the
# default Neo4j database. GPC consolidates data by project_slug, and each
# project may contain multiple Git repositories separated by repo_slug.

set -u

if [ "${GPC_GRAPHIFY_ENABLED:-1}" = "0" ]; then
  exit 0
fi

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$PROJECT_ROOT" || exit 0

mkdir -p .gpc
LOG_PATH="${GPC_GRAPHIFY_LOG:-.gpc/graphify-neo4j.log}"

run_graphify_neo4j_sync() {
  LOCK_DIR=".gpc/graphify-neo4j.lock"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] graphify neo4j sync started"

  GRAPHIFY_PYTHON=""
  GRAPHIFY_BIN="$(command -v graphify 2>/dev/null || true)"
  if [ -n "$GRAPHIFY_BIN" ]; then
    _SHEBANG="$(head -1 "$GRAPHIFY_BIN" | sed 's/^#![[:space:]]*//')"
    case "$_SHEBANG" in
      */env\ *) GRAPHIFY_PYTHON="${_SHEBANG#*/env }" ;;
      *) GRAPHIFY_PYTHON="$_SHEBANG" ;;
    esac
    case "$GRAPHIFY_PYTHON" in
      *[!a-zA-Z0-9/_.@-]*) GRAPHIFY_PYTHON="" ;;
    esac
    if [ -n "$GRAPHIFY_PYTHON" ] && ! "$GRAPHIFY_PYTHON" -c "import graphify" 2>/dev/null; then
      GRAPHIFY_PYTHON=""
    fi
  fi

  if [ -n "$GRAPHIFY_PYTHON" ]; then
    "$GRAPHIFY_PYTHON" - <<'PY'
from pathlib import Path
from graphify.watch import _rebuild_code

_rebuild_code(Path("."))
PY
  elif command -v graphify >/dev/null 2>&1; then
    graphify update .
  else
    echo "[graphify neo4j] graphify is not installed; skipping"
    exit 0
  fi

  if [ ! -f graphify-out/graph.json ]; then
    echo "[graphify neo4j] graphify-out/graph.json not found; run graphify once before enabling this hook"
    exit 0
  fi

  NEO4J_PYTHON="${GPC_GRAPHIFY_NEO4J_PYTHON:-}"
  if [ -z "$NEO4J_PYTHON" ]; then
    for candidate in "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/.venv/bin/python" python3 python "$GRAPHIFY_PYTHON"; do
      if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import neo4j" 2>/dev/null; then
        NEO4J_PYTHON="$candidate"
        break
      fi
    done
  fi

  if [ -z "$NEO4J_PYTHON" ]; then
    echo "[graphify neo4j] Python neo4j driver not found; set GPC_GRAPHIFY_NEO4J_PYTHON"
    exit 0
  fi

  "$NEO4J_PYTHON" - <<'PY'
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from neo4j import GraphDatabase


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-_").lower()
    return slug or "project"


def prop_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=True)[:4000]
    return str(value)


project_slug = slugify(
    os.environ.get("GPC_GRAPHIFY_PROJECT_SLUG")
    or os.environ.get("GPC_PROJECT_SLUG")
    or Path.cwd().name
)
repo_slug = slugify(os.environ.get("GPC_GRAPHIFY_REPO_SLUG") or Path.cwd().name)

uri = os.environ.get("GPC_GRAPHIFY_NEO4J_URI", os.environ.get("GPC_NEO4J_URI", "bolt://localhost:7687"))
user = os.environ.get("GPC_GRAPHIFY_NEO4J_USER", os.environ.get("GPC_NEO4J_USER", "neo4j"))
password = os.environ.get(
    "GPC_GRAPHIFY_NEO4J_PASSWORD",
    os.environ.get("GPC_NEO4J_PASSWORD", "gpcneo4jpass"),
)

graph_path = Path(os.environ.get("GPC_GRAPHIFY_GRAPH_PATH", "graphify-out/graph.json"))
data = json.loads(graph_path.read_text(encoding="utf-8"))
nodes = data.get("nodes", [])
links = data.get("links", data.get("edges", []))

driver = GraphDatabase.driver(uri, auth=(user, password))

try:
    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT graphify_project_slug IF NOT EXISTS "
            "FOR (p:GraphifyProject) REQUIRE p.slug IS UNIQUE"
        )
        session.run(
            "CREATE CONSTRAINT graphify_repo_id IF NOT EXISTS "
            "FOR (r:GraphifyRepo) REQUIRE r.id IS UNIQUE"
        )
        session.run(
            "CREATE CONSTRAINT graphify_node_id IF NOT EXISTS "
            "FOR (n:GraphifyNode) REQUIRE n.id IS UNIQUE"
        )

        clear_repo = os.environ.get(
            "GPC_GRAPHIFY_NEO4J_CLEAR_REPO",
            os.environ.get("GPC_GRAPHIFY_NEO4J_CLEAR_PROJECT", "1"),
        )
        if clear_repo == "1":
            session.run(
                """
                MATCH (repo:GraphifyRepo {id: $repo_id})-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                DETACH DELETE n
                """,
                repo_id=f"{project_slug}:{repo_slug}",
            )

        session.run(
            """
            MERGE (p:GraphifyProject {slug: $project_slug})
            SET p.updated_at = datetime(), p.source = "gpc"
            MERGE (r:GraphifyRepo {id: $repo_id})
            SET
                r.slug = $repo_slug,
                r.project_slug = $project_slug,
                r.root_path = $root_path,
                r.updated_at = datetime(),
                r.source = "graphify"
            MERGE (p)-[:HAS_REPO]->(r)
            """,
            project_slug=project_slug,
            repo_id=f"{project_slug}:{repo_slug}",
            repo_slug=repo_slug,
            root_path=str(Path.cwd()),
        )

        node_rows = []
        for node in nodes:
            source_id = str(node.get("id") or node.get("label"))
            global_id = f"{project_slug}:{repo_slug}:{source_id}"
            props = {k: prop_value(v) for k, v in node.items() if k != "id"}
            props.update(
                {
                    "id": global_id,
                    "source_id": source_id,
                    "project_slug": project_slug,
                    "repo_slug": repo_slug,
                    "repo_id": f"{project_slug}:{repo_slug}",
                    "source": "graphify",
                }
            )
            node_rows.append({"id": global_id, "props": props})

        edge_rows = []
        for index, link in enumerate(links):
            source = str(link.get("source") or link.get("_src") or "")
            target = str(link.get("target") or link.get("_tgt") or "")
            if not source or not target:
                continue
            relation = str(link.get("relation") or link.get("label") or "RELATED_TO")
            edge_id = f"{project_slug}:{repo_slug}:{source}:{relation}:{target}:{index}"
            props = {k: prop_value(v) for k, v in link.items() if k not in {"source", "target", "_src", "_tgt"}}
            props.update(
                {
                    "id": edge_id,
                    "project_slug": project_slug,
                    "repo_slug": repo_slug,
                    "repo_id": f"{project_slug}:{repo_slug}",
                    "relation": relation,
                    "source": "graphify",
                }
            )
            edge_rows.append(
                {
                    "id": edge_id,
                    "source_id": f"{project_slug}:{repo_slug}:{source}",
                    "target_id": f"{project_slug}:{repo_slug}:{target}",
                    "props": props,
                }
            )

        session.run(
            """
            UNWIND $rows AS row
            MERGE (n:GraphifyNode {id: row.id})
            SET n += row.props
            """,
            rows=node_rows,
        )
        session.run(
            """
            MATCH (repo:GraphifyRepo {id: $repo_id})
            MATCH (n:GraphifyNode {repo_id: $repo_id})
            MERGE (repo)-[:HAS_GRAPH_NODE]->(n)
            """,
            repo_id=f"{project_slug}:{repo_slug}",
        )
        session.run(
            """
            UNWIND $rows AS row
            MATCH (a:GraphifyNode {id: row.source_id})
            MATCH (b:GraphifyNode {id: row.target_id})
            MERGE (a)-[r:GRAPHIFY_RELATION {id: row.id}]->(b)
            SET r += row.props
            """,
            rows=edge_rows,
        )

    print(
        f"[graphify neo4j] project={project_slug} repo={repo_slug} database=neo4j "
        f"nodes={len(node_rows)} edges={len(edge_rows)}"
    )
finally:
    driver.close()
PY

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] graphify neo4j sync finished"
}

( run_graphify_neo4j_sync ) >> "$LOG_PATH" 2>&1 &
exit 0
