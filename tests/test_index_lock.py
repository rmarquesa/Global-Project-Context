from __future__ import annotations

import inspect
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from gpc import indexer
from gpc.index_lock import RepositoryBusyError, repository_lock


def test_collision_alias_and_stable_empty_lock(tmp_path):
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        with repository_lock(tmp_path):
            lock = tmp_path / ".gpc/index.lock"
            inode = lock.stat().st_ino
            with pytest.raises(RepositoryBusyError):
                with repository_lock(alias):
                    pytest.fail("alias bypassed lock")
        assert lock.read_bytes() == b""
        with repository_lock(tmp_path):
            assert lock.stat().st_ino == inode
    finally:
        alias.unlink()


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_release_on_exception(tmp_path, error):
    with pytest.raises(error):
        with repository_lock(tmp_path):
            raise error()
    with repository_lock(tmp_path):
        pass


def test_roots_and_lock_names_are_independent(tmp_path):
    other = tmp_path / "other-worktree"
    other.mkdir()
    with repository_lock(tmp_path):
        with repository_lock(tmp_path, name="watch"):
            with repository_lock(other):
                pass


@pytest.mark.parametrize("name", ["../escape", "", "anything", "index/else"])
def test_names_are_restricted(tmp_path, name):
    with pytest.raises(ValueError):
        with repository_lock(tmp_path, name=name):
            pass
    assert not (tmp_path / ".gpc").exists()


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_unsafe_metadata_rejected(tmp_path, kind):
    target = tmp_path / "target"
    target.mkdir()
    if kind == "symlink":
        (tmp_path / ".gpc").symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / ".gpc").write_text("preserve")
    with pytest.raises((ValueError, OSError)):
        with repository_lock(tmp_path):
            pass
    assert not list(target.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_lock_rejected_without_writing(tmp_path, kind):
    metadata = tmp_path / ".gpc"
    metadata.mkdir()
    lock = metadata / "index.lock"
    target = tmp_path / "target"
    target.write_text("must not truncate")
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, lock)
    elif kind == "directory":
        lock.mkdir()
    elif hasattr(os, "mkfifo"):
        os.mkfifo(lock)
    else:
        pytest.skip("FIFO unsupported")
    with pytest.raises((ValueError, OSError)):
        with repository_lock(tmp_path):
            pass
    assert target.read_text() == "must not truncate"


def test_invalid_root_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        with repository_lock(tmp_path / "missing"):
            pass
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(NotADirectoryError):
        with repository_lock(file):
            pass


def test_real_process_collision_and_exit_release(tmp_path, monkeypatch):
    script = """
import sys
from gpc.index_lock import repository_lock
with repository_lock(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
    # Bypass Python finally blocks to exercise OS process-exit release.
    import os
    os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline() == "locked\n"
        backend = Mock(side_effect=AssertionError("backend called while busy"))
        monkeypatch.setattr(indexer, "register_project", backend)
        with pytest.raises(RepositoryBusyError):
            with repository_lock(tmp_path):
                pass
        with pytest.raises(RepositoryBusyError):
            indexer.index_project_path(tmp_path)
        backend.assert_not_called()
        process.communicate("exit\n", timeout=10)
        assert process.returncode == 0
        with repository_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor failure injection")
def test_descriptors_closed_if_lock_acquisition_errors(tmp_path, monkeypatch):
    import fcntl

    opened = []
    real_open = os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(fcntl, "flock", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        with repository_lock(tmp_path):
            pass
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_public_indexer_blocks_before_any_backend_call(tmp_path, monkeypatch):
    backend = Mock(side_effect=AssertionError("backend called while busy"))
    for name in ("register_project", "register_repo", "register_source", "get_qdrant"):
        monkeypatch.setattr(indexer, name, backend)
    with repository_lock(tmp_path):
        with pytest.raises(RepositoryBusyError):
            indexer.index_project_path(tmp_path)
    backend.assert_not_called()


def test_public_indexer_forwards_signature_options_and_stats(tmp_path, monkeypatch):
    stats = object()
    options = indexer.IndexOptions()

    def implementation(root, **kwargs):
        assert root == tmp_path
        assert kwargs == dict(
            slug="project",
            name="display",
            description="description",
            options=options,
            repo_slug="repo",
        )
        with pytest.raises(RepositoryBusyError):
            with repository_lock(root):
                pass
        return stats

    monkeypatch.setattr(indexer, "_index_project_path", implementation)
    assert list(inspect.signature(indexer.index_project_path).parameters) == [
        "root_path",
        "slug",
        "name",
        "description",
        "options",
        "repo_slug",
    ]
    assert (
        indexer.index_project_path(
            tmp_path,
            slug="project",
            name="display",
            description="description",
            options=options,
            repo_slug="repo",
        )
        is stats
    )
    with repository_lock(tmp_path):
        pass


def test_public_indexer_releases_after_backend_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        indexer, "register_project", Mock(side_effect=RuntimeError("fail"))
    )
    with pytest.raises(RuntimeError, match="fail"):
        indexer.index_project_path(tmp_path)
    with repository_lock(tmp_path):
        pass
