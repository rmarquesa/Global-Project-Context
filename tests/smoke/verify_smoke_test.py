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
            "verify",
            "--project",
            "gpc",
            "--quick",
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["project_slug"] == "gpc", payload
    assert payload["overall_status"] in {"pass", "warn"}, payload
    assert payload["summary"]["fail"] == 0, payload
    names = {check["name"] for check in payload["checks"]}
    assert "project_resolution" in names, payload
    assert "index_state" in names, payload
    assert "graphify_report" in names, payload
    assert "staleness" in names, payload
    assert "graph_summary" in names, payload
    print("verify_smoke_test=passed")


if __name__ == "__main__":
    main()
