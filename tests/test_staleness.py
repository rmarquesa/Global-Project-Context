from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from contextlib import contextmanager
import subprocess

import pytest

from gpc import staleness

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


@pytest.fixture
def project_state(tmp_path, monkeypatch):
    """Real diagnostic, hash/filesystem and SQL boundary; no registry/store I/O."""
    root = tmp_path / "logical-root-does-not-exist"
    project = {"id": "project-id", "slug": "logical", "root_path": str(root)}
    repos = []
    rows = []
    git_paths = {}
    queries = []
    monkeypatch.setattr(staleness, "resolve_project", lambda **kwargs: project)

    def list_repos(slug):
        assert slug == "logical"
        return repos

    monkeypatch.setattr(staleness, "list_repos", list_repos)

    class Connection:
        def execute(self, query, params):
            assert query.startswith("select ")
            assert "repo_id is not distinct from %s" in query
            assert params[0] == "project-id"
            queries.append(params)
            self.result = [row for row in rows if row["repo_id"] == params[1]]
            return self

        def fetchall(self):
            return self.result

    @contextmanager
    def connection(**kwargs):
        yield Connection()

    monkeypatch.setattr(staleness, "pg_connection", connection)

    def git_lines(path, args):
        if path not in git_paths:
            raise subprocess.CalledProcessError(128, ["git", *args])
        return git_paths[path]

    monkeypatch.setattr(staleness, "_git_lines", git_lines)

    def add_repo(slug, files, *, registered=True, indexed=True):
        repo_root = tmp_path / slug
        repo_root.mkdir()
        repo_id = f"{slug}-id" if registered else None
        if registered:
            repos.append({"id": repo_id, "slug": slug, "root_path": str(repo_root)})
        else:
            project["root_path"] = str(repo_root)
        git_paths[repo_root] = list(files)
        for path, text in files.items():
            full_path = repo_root / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(text, encoding="utf-8")
            if indexed:
                rows.append(
                    {
                        "repo_id": repo_id,
                        "relative_path": path,
                        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                        "indexed_at": datetime.now(timezone.utc),
                    }
                )
        return repo_root

    return project, repos, rows, git_paths, queries, add_repo


@pytest.mark.parametrize(
    "parent_exists", [False, True], ids=["missing-root", "non-git-root"]
)
def test_multirepo_same_paths_and_repo_unique_file_are_fresh(
    project_state, parent_exists
):
    project, repos, rows, git_paths, queries, add_repo = project_state
    if parent_exists:
        from pathlib import Path

        Path(project["root_path"]).mkdir()
    a = add_repo("a", {"README.md": "A"})
    add_repo("b", {"README.md": "B", "only-b.py": "print('B')"})

    # Caller cwd is one repo; the logical project root is deliberately absent.
    report = staleness.detect_project_staleness(cwd=str(a))

    assert report.is_stale is False
    assert report.deleted_but_indexed == []
    assert set(queries) == {
        ("project-id", "a-id"),
        ("project-id", "b-id"),
        ("project-id", None),
    }


def test_multirepo_change_is_qualified_and_does_not_collide(project_state):
    *_, add_repo = project_state
    add_repo("a", {"README.md": "A"})
    b = add_repo("b", {"README.md": "B", "only-b.py": "print('B')"})
    (b / "README.md").write_text("changed", encoding="utf-8")

    report = staleness.detect_project_staleness(cwd=str(b))

    assert report.is_stale
    assert report.modified_since_index == ["b:README.md"]
    assert report.missing_from_index == []
    assert report.deleted_but_indexed == []


def test_multirepo_missing_and_deleted_paths_are_qualified(project_state):
    project, repos, rows, git_paths, queries, add_repo = project_state
    add_repo("a", {"README.md": "A"})
    add_repo("b", {"Dockerfile": "FROM scratch"})
    rows[1]["relative_path"] = "deleted.py"

    report = staleness.detect_project_staleness()

    assert report.missing_from_index == ["b:Dockerfile"]
    assert report.deleted_but_indexed == ["b:deleted.py"]


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "non-git"])
def test_unavailable_registered_repo_is_explicitly_nonfresh(project_state, exists):
    project, repos, rows, git_paths, queries, add_repo = project_state
    a = add_repo("a", {"README.md": "A"})
    bad = a.parent / "unavailable"
    if exists:
        bad.mkdir()
    repos.append({"id": "bad-id", "slug": "bad", "root_path": str(bad)})

    report = staleness.detect_project_staleness()

    assert report.is_stale
    assert report.unavailable_repos == ["bad"]
    assert report.to_dict()["unavailable_repos"] == ["bad"]
    assert report.summary["unavailable_repos"] == 1
    assert report.to_dict()["remediation"]
    assert report.deleted_but_indexed == []


@pytest.mark.parametrize("registered", [False, True], ids=["legacy", "single-repo"])
def test_single_repo_paths_remain_unqualified(project_state, registered):
    project, repos, rows, git_paths, queries, add_repo = project_state
    root = add_repo("single", {"README.md": "original"}, registered=registered)
    assert staleness.detect_project_staleness().is_stale is False
    (root / "README.md").write_text("changed", encoding="utf-8")

    report = staleness.detect_project_staleness()

    assert report.modified_since_index == ["README.md"]
    assert report.deleted_but_indexed == []
    assert ("project-id", "single-id" if registered else None) in queries


def test_registered_repo_does_not_silently_adopt_legacy_null_rows(project_state):
    project, repos, rows, git_paths, queries, add_repo = project_state
    add_repo("a", {"README.md": "A"})
    rows.append({**rows[0], "repo_id": None, "content_hash": "legacy hash"})

    report = staleness.detect_project_staleness()

    assert report.is_stale
    assert report.modified_since_index == []
    assert report.unassigned_indexed_files == ["README.md"]
    assert report.to_dict()["unassigned_indexed_files"] == ["README.md"]
    assert report.summary["unassigned_indexed_files"] == 1
    assert any("legacy" in item for item in report.to_dict()["remediation"])


@pytest.mark.parametrize(
    "path", [".git/ignored.py", ".venv/ignored.py", "./.gpc/ignored.py"]
)
def test_relevance_preserves_leading_dots_in_ignored_directories(path):
    assert staleness.is_relevant_path(path) is False
