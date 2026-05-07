from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gpc.staleness import analyze_staleness, _modified_after_index


def test_analyze_staleness_detects_missing_deleted_and_modified_files() -> None:
    report = analyze_staleness(
        tracked_files={"README.md", "gpc/cli.py", "gpc/new.py"},
        indexed_files={"README.md", "gpc/cli.py", "gpc/deleted.py"},
        modified_files={"gpc/cli.py", "graphify-out/graph.html", "venv/ignored.py"},
        graphify_report_is_stale=True,
    )

    assert report.is_stale is True
    assert report.summary["missing_from_index"] == 1
    assert report.summary["deleted_but_indexed"] == 1
    assert report.summary["modified_since_index"] == 1
    assert report.summary["graphify_report_stale"] == 1
    assert report.missing_from_index == ["gpc/new.py"]
    assert report.deleted_but_indexed == ["gpc/deleted.py"]
    assert report.modified_since_index == ["gpc/cli.py"]


def test_analyze_staleness_ignores_generated_and_untracked_irrelevant_paths() -> None:
    report = analyze_staleness(
        tracked_files={
            "README.md",
            "graphify-out/GRAPH_REPORT.md",
            "site/og-image.png",
            ".graphifyignore",
        },
        indexed_files={"README.md"},
        modified_files={
            ".gpc/index.log",
            "__pycache__/x.pyc",
            "graphify-out/graph.json",
        },
    )

    assert report.is_stale is False
    assert report.missing_from_index == []
    assert report.modified_since_index == []


def test_modified_after_index_uses_file_mtime_not_git_porcelain(tmp_path) -> None:
    fresh = tmp_path / "fresh.py"
    stale = tmp_path / "stale.py"
    fresh.write_text("print('fresh')\n", encoding="utf-8")
    stale.write_text("print('stale')\n", encoding="utf-8")

    now = datetime.now(timezone.utc)
    indexed_info = {
        "fresh.py": now + timedelta(hours=1),
        "stale.py": now - timedelta(hours=1),
        "missing.py": now - timedelta(hours=1),
        "image.png": now - timedelta(hours=1),
    }

    assert _modified_after_index(tmp_path, indexed_info) == ["stale.py"]
