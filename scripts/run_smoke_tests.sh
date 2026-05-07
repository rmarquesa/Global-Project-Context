#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python executable not found or not executable: $PYTHON_BIN" >&2
  echo "Set PYTHON_BIN=/path/to/python or run ./install.sh first." >&2
  exit 1
fi

cd "$ROOT_DIR"

# Order matters: embedding verifies the provider first, then search refreshes
# and validates the Qdrant bootstrap seed before the DB/Neo4j/MCP checks.
# Graph projection/query checks run before graph self-visibility, graph-quality,
# and self-metrics checks so the Graphify/Neo4j read model is coherent.
smoke_tests=(
  tests.smoke.embedding_smoke_test
  tests.smoke.search_test
  tests.smoke.registry_smoke_test
  tests.smoke.graph_projection_smoke_test
  tests.smoke.graph_query_smoke_test
  tests.smoke.graph_self_visibility_smoke_test
  tests.smoke.verify_smoke_test
  tests.smoke.search_eval_smoke_test
  tests.smoke.context_pack_smoke_test
  tests.smoke.health_report_smoke_test
  tests.smoke.mcp_usage_smoke_test
  tests.smoke.maintenance_smoke_test
  tests.smoke.graph_quality_smoke_test
  tests.smoke.self_metrics_smoke_test
  tests.smoke.mcp_observability_smoke_test
  tests.smoke.mcp_smoke_test
)

for module in "${smoke_tests[@]}"; do
  echo "==> $module"
  "$PYTHON_BIN" -m "$module"
done

echo "==> smoke tests passed"
