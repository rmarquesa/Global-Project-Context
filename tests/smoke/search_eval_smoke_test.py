from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gpc.cli",
            "eval-search",
            "--project",
            "gpc",
            "--fixture",
            "tests/fixtures/search_eval_gpc.yml",
            "--k",
            "5",
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["embedding"]["provider"] == "ollama", payload
    assert payload["embedding"]["model"] == "nomic-embed-text:latest", payload
    assert payload["embedding"]["dimensions"] == 768, payload
    assert payload["summary"]["queries"] >= 1, payload
    assert 0.0 <= payload["summary"]["recall_at_k"] <= 1.0, payload
    print("search_eval_smoke_test=passed")


if __name__ == "__main__":
    main()
