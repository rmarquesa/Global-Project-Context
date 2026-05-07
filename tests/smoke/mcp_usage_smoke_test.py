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
            "mcp-usage",
            "--project",
            "gpc",
            "--since",
            "24h",
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["project"] == "gpc", payload
    assert "by_tool" in payload, payload
    rendered = json.dumps(payload).lower()
    assert "hunter2" not in rendered
    assert "api_key" not in rendered or "[redacted]" in rendered
    print("mcp_usage_smoke_test=passed")


if __name__ == "__main__":
    main()
