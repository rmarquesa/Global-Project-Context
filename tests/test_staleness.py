from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from gpc.staleness import (
    analyze_staleness,
    _filter_present_indexable_files,
    _modified_after_index,
)


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


def test_modified_after_index_compares_content_hash_before_mtime(tmp_path) -> None:
    unchanged = tmp_path / "unchanged.py"
    changed = tmp_path / "changed.py"
    unchanged_text = "print('same')\n"
    unchanged.write_text(unchanged_text, encoding="utf-8")
    changed.write_text("print('new')\n", encoding="utf-8")

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=1)
    indexed_info = {
        "unchanged.py": {
            "indexed_at": old,
            "content_hash": hashlib.sha256(unchanged_text.encode()).hexdigest(),
        },
        "changed.py": {
            "indexed_at": old,
            "content_hash": hashlib.sha256(b"print('old')\n").hexdigest(),
        },
        "missing.py": {"indexed_at": old, "content_hash": "missing"},
        "image.png": {"indexed_at": old, "content_hash": "ignored"},
    }

    assert _modified_after_index(tmp_path, indexed_info) == ["changed.py"]


def test_modified_after_index_falls_back_to_mtime_without_hash(tmp_path) -> None:
    fresh = tmp_path / "fresh.py"
    stale = tmp_path / "stale.py"
    fresh.write_text("print('fresh')\n", encoding="utf-8")
    stale.write_text("print('stale')\n", encoding="utf-8")

    now = datetime.now(timezone.utc)
    indexed_info = {
        "fresh.py": {"indexed_at": now + timedelta(hours=1), "content_hash": None},
        "stale.py": {"indexed_at": now - timedelta(hours=1), "content_hash": None},
    }

    assert _modified_after_index(tmp_path, indexed_info) == ["stale.py"]


def test_filter_present_indexable_files_ignores_empty_supported_files(tmp_path) -> None:
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "nonempty.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"PNG")

    assert _filter_present_indexable_files(
        tmp_path, ["empty.json", "nonempty.json", "image.png"]
    ) == ["nonempty.json"]
