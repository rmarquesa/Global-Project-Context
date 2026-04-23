#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${GPC_VENV_DIR:-$ROOT_DIR/venv}"
BIN_DIR="${GPC_BIN_DIR:-$HOME/.local/bin}"
CLIENTS="all"
SKIP_DOCKER=0
SKIP_OLLAMA=0
SKIP_CLIENTS=0
SKIP_SMOKE=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bin-dir PATH        Install command shims into PATH (default: ~/.local/bin)
  --clients LIST        MCP clients to configure (default: all)
  --skip-docker         Do not start Docker Compose services
  --skip-ollama         Do not pull the Ollama embedding model
  --skip-clients        Do not configure MCP clients
  --skip-smoke          Do not run MCP smoke test
  -h, --help            Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bin-dir)
      BIN_DIR="$2"
      shift 2
      ;;
    --clients)
      CLIENTS="$2"
      shift 2
      ;;
    --skip-docker)
      SKIP_DOCKER=1
      shift
      ;;
    --skip-ollama)
      SKIP_OLLAMA=1
      shift
      ;;
    --skip-clients)
      SKIP_CLIENTS=1
      shift
      ;;
    --skip-smoke)
      SKIP_SMOKE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

echo "==> preparing Python environment"
require_command "$PYTHON_BIN"
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  echo "created .env from .env.example"
fi

if [ "$SKIP_DOCKER" -eq 0 ]; then
  echo "==> starting local databases"
  require_command docker
  (cd "$ROOT_DIR" && docker compose up -d)
fi

if [ "$SKIP_OLLAMA" -eq 0 ]; then
  echo "==> preparing Ollama embedding model"
  require_command ollama
  ollama pull "${GPC_OLLAMA_EMBEDDING_MODEL:-nomic-embed-text:latest}"
fi

echo "==> applying database and vector setup"
(cd "$ROOT_DIR" && "$VENV_DIR/bin/python" -m scripts.migrate up)
(cd "$ROOT_DIR" && "$VENV_DIR/bin/python" -m scripts.init_qdrant)

echo "==> installing global GPC commands"
(cd "$ROOT_DIR" && "$VENV_DIR/bin/python" -m gpc.cli install-shims --bin-dir "$BIN_DIR")

if [ "$SKIP_CLIENTS" -eq 0 ]; then
  echo "==> configuring MCP clients"
  CLIENT_ARGS=("--clients" "$CLIENTS")
  if [ "$SKIP_SMOKE" -eq 1 ]; then
    CLIENT_ARGS+=("--skip-smoke")
  fi
  (cd "$ROOT_DIR" && "$VENV_DIR/bin/python" -m scripts.install_mcp_clients "${CLIENT_ARGS[@]}")
fi

if [ "$SKIP_SMOKE" -eq 0 ]; then
  echo "==> validating MCP server"
  (cd "$ROOT_DIR" && "$VENV_DIR/bin/python" -m tests.smoke.mcp_smoke_test)
fi

echo "==> GPC installation complete"
echo "Commands installed in: $BIN_DIR"
echo "Try: gpc doctor"

