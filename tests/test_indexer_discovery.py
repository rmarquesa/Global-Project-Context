from __future__ import annotations

from collections import Counter
from pathlib import Path
import subprocess

import pytest

from gpc import indexer

from gpc.indexer import (
    IndexOptions,
    _candidate_from_path,
    _looks_sensitive_filename,
    _matches_gitignore,
)


def test_candidate_from_path_skips_sensitive_files_and_allows_safe_env_examples(
    tmp_path,
) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")
    sample = tmp_path / ".env.example"
    sample.write_text("TOKEN=example", encoding="utf-8")
    skipped: Counter[str] = Counter()

    assert _candidate_from_path(tmp_path, secret, IndexOptions(), skipped) is None
    candidate = _candidate_from_path(tmp_path, sample, IndexOptions(), skipped)

    assert skipped["sensitive_filename"] == 1
    assert candidate is not None
    assert candidate.relative_path == ".env.example"


def test_candidate_from_path_respects_unknown_text_policy(tmp_path) -> None:
    unknown = tmp_path / "notes.custom"
    unknown.write_text("plain text", encoding="utf-8")

    skipped: Counter[str] = Counter()
    assert _candidate_from_path(tmp_path, unknown, IndexOptions(), skipped) is None
    assert skipped["unsupported_extension"] == 1

    allowed = _candidate_from_path(
        tmp_path,
        unknown,
        IndexOptions(include_unknown_text=True),
        Counter(),
    )
    assert allowed is not None
    assert allowed.relative_path == "notes.custom"


def test_gitignore_matching_handles_anchored_directory_and_basename_patterns() -> None:
    assert _matches_gitignore("build", is_dir=True, patterns=["build/"])
    assert not _matches_gitignore("src/build.py", is_dir=False, patterns=["build/"])
    assert _matches_gitignore(
        "src/generated/output.py", is_dir=False, patterns=["/src/generated/*"]
    )
    assert _matches_gitignore("docs/tmp/cache.txt", is_dir=False, patterns=["tmp"])


def test_sensitive_filename_policy_keeps_examples_but_blocks_private_keys() -> None:
    assert _looks_sensitive_filename(".env.example") is False
    assert _looks_sensitive_filename("id_rsa") is True
    assert _looks_sensitive_filename("service.secret") is True


@pytest.fixture(params=[False, True], ids=["walk", "git"])
def discovery_root(tmp_path, request):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    if request.param:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root, request.param


def track(root: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "--", *paths], check=True)


def discovered(root: Path, **options) -> set[str]:
    return {
        candidate.relative_path
        for candidate in indexer.discover_project_files(
            root, IndexOptions(**options)
        ).candidates
    }


def test_file_symlink_identity_survives_git_discovery(discovery_root) -> None:
    root, is_git = discovery_root
    (root / "safe.txt").write_text("public documentation")
    (root / "public.txt").symlink_to("safe.txt")
    if is_git:
        track(root, "safe.txt", "public.txt")
        paths = indexer._git_discover_paths(root)
        assert root / "public.txt" in paths

    assert "safe.txt" in discovered(root)
    assert "public.txt" not in discovered(root)
    assert "public.txt" in discovered(root, follow_symlinks=True)


@pytest.mark.parametrize("follow", [False, True])
def test_symlink_cannot_alias_an_ignored_target(discovery_root, follow) -> None:
    root, is_git = discovery_root
    (root / ".gitignore").write_text("private/\n")
    (root / "private").mkdir()
    (root / "private" / "settings.txt").write_text("private non-secret notes")
    (root / "public.txt").symlink_to("private/settings.txt")
    (root / "public-dir").symlink_to("private", target_is_directory=True)
    if is_git:
        track(root, ".gitignore", "public.txt", "public-dir")

    paths = discovered(root, follow_symlinks=follow)
    assert paths == {".gitignore"}


def test_directory_symlink_and_ancestor_policy(discovery_root) -> None:
    root, is_git = discovery_root
    (root / "docs").mkdir()
    (root / "docs" / "guide.txt").write_text("public guide")
    (root / "alias").symlink_to("docs", target_is_directory=True)
    if is_git:
        track(root, "docs/guide.txt", "alias")

    alias_file = root / "alias" / "guide.txt"
    assert (
        indexer._candidate_from_path(root, alias_file, IndexOptions(), Counter())
        is None
    )
    candidate = indexer._candidate_from_path(
        root, alias_file, IndexOptions(follow_symlinks=True), Counter()
    )
    assert candidate is not None
    assert candidate.relative_path == "alias/guide.txt"
    assert "alias/guide.txt" not in discovered(root)
    assert "alias/guide.txt" in discovered(root, follow_symlinks=True)


@pytest.mark.parametrize("follow", [False, True])
def test_external_symlink_files_and_directories_always_rejected(
    discovery_root, follow, monkeypatch
) -> None:
    root, is_git = discovery_root
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("outside root")
    (root / "file.txt").symlink_to(outside / "notes.txt")
    (root / "directory").symlink_to(outside, target_is_directory=True)
    if is_git:
        track(root, "file.txt", "directory")

    options = IndexOptions(follow_symlinks=follow)
    for path in [
        root / "file.txt",
        root / "directory" / "notes.txt",
        outside / "notes.txt",
    ]:
        skipped: Counter[str] = Counter()
        assert indexer._candidate_from_path(root, path, options, skipped) is None
        assert skipped["outside_root"] == 1

    visited = []
    real_candidate = indexer._candidate_from_path

    def observe(root, path, options, skipped):
        visited.append(path)
        return real_candidate(root, path, options, skipped)

    monkeypatch.setattr(indexer, "_candidate_from_path", observe)
    assert discovered(root, follow_symlinks=follow) == set()
    assert root / "directory" / "notes.txt" not in visited


def test_in_root_symlink_cannot_alias_sensitive_filename(discovery_root) -> None:
    root, is_git = discovery_root
    (root / ".env").write_text("synthetic=short")
    (root / "public.txt").symlink_to(".env")
    if is_git:
        track(root, "public.txt")
    assert discovered(root, follow_symlinks=True) == set()


def test_link_target_respects_nested_gitignore_and_tracked_ignored_files(
    tmp_path,
) -> None:
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    nested = root / "nested"
    nested.mkdir()
    (nested / "notes.txt").write_text("private non-secret notes")
    track(root, "nested/notes.txt")
    (nested / ".gitignore").write_text("notes.txt\n")
    (root / "public.txt").symlink_to("nested/notes.txt")
    track(root, "public.txt", "nested/.gitignore")

    assert "public.txt" not in discovered(root, follow_symlinks=True)


def test_explicit_gitignore_exception_allows_opted_in_target(tmp_path) -> None:
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("notes.txt\n!nested/notes.txt\n")
    (root / "nested").mkdir()
    (root / "nested" / "notes.txt").write_text("public notes")
    (root / "public.txt").symlink_to("nested/notes.txt")
    track(root, "public.txt")

    assert "public.txt" in discovered(root, follow_symlinks=True)


def test_symlink_ignore_policy_check_fails_closed(discovery_root, monkeypatch) -> None:
    root, _ = discovery_root
    (root / "safe.txt").write_text("public notes")
    (root / "public.txt").symlink_to("safe.txt")
    run = indexer.subprocess.run
    checks = []

    def fail_check(command, **kwargs):
        if "check-ignore" in command:
            checks.append(command)
            raise subprocess.TimeoutExpired(command, 5)
        return run(command, **kwargs)

    monkeypatch.setattr(indexer.subprocess, "run", fail_check)
    assert "public.txt" not in discovered(root, follow_symlinks=True)
    assert checks


def test_symlink_directory_cycle_is_pruned(discovery_root) -> None:
    root, is_git = discovery_root
    (root / "safe.txt").write_text("public notes")
    (root / "loop").symlink_to(".", target_is_directory=True)
    if is_git:
        track(root, "safe.txt", "loop")

    paths = discovered(root, follow_symlinks=True)
    assert "safe.txt" in paths
    assert all("loop/loop/" not in path for path in paths)
    assert len(paths) <= 2


def test_repository_root_can_itself_be_a_symlink(discovery_root) -> None:
    root, _ = discovery_root
    (root / "safe.txt").write_text("public notes")
    root_alias = root.parent / "repo-alias"
    root_alias.symlink_to(root, target_is_directory=True)

    result = indexer.discover_project_files(root_alias, IndexOptions())
    assert [candidate.relative_path for candidate in result.candidates] == ["safe.txt"]
    assert result.candidates[0].absolute_path == root / "safe.txt"
