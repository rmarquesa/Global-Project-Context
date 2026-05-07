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
            "maintenance",
            "doctor",
            "--project",
            "gpc",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    payload = json.loads(result.stdout)
    assert payload["project_slug"] == "gpc", payload
    assert payload["dry_run"] is True, payload
    assert "findings" in payload, payload
    print("maintenance_smoke_test=passed")


if __name__ == "__main__":
    main()
