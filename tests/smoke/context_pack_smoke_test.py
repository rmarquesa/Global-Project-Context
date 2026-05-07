from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".gpc" / "smoke-context-pack.md"


def main() -> None:
    if OUTPUT.exists():
        OUTPUT.unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gpc.cli",
            "context-pack",
            "MCP read-only architecture",
            "--project",
            "gpc",
            "--max-chunks",
            "2",
            "--max-chars",
            "2500",
            "--output",
            str(OUTPUT),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "context_pack=" in result.stdout, result.stdout
    markdown = OUTPUT.read_text(encoding="utf-8")
    assert "# Context Pack" in markdown
    assert "## Citations" in markdown
    print("context_pack_smoke_test=passed")


if __name__ == "__main__":
    main()
