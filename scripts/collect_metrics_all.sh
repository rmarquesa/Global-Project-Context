#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-$ROOT_DIR/.venv/bin/python}"
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  echo "Set PYTHON_BIN=/path/to/python or run ./install.sh first." >&2
  exit 1
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m gpc.cli metrics collect-all "$@"
