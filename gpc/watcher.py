"""Optional, foreground polling of one registered Git worktree.

Only stat metadata and Git identity are retained; the incremental indexer remains
responsible for content hashing. There is one synchronous index call and at most
one pending reconciliation. No watcher dependency or background service is used.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable

from gpc.index_lock import RepositoryBusyError, repository_lock
from gpc.indexer import IndexOptions, _build_discovery, index_project_path
from gpc.registry import resolve_repo


@dataclass(frozen=True)
class WatchOptions:
    """All delays are finite and positive, except debounce may be zero."""

    poll_interval: float = 1.0
    debounce: float = 1.0
    max_debounce: float = 10.0
    retry_interval: float = 5.0

    def __post_init__(self) -> None:
        for name in ("poll_interval", "debounce", "max_debounce", "retry_interval"):
            value = getattr(self, name)
            if (
                not math.isfinite(value)
                or value < 0
                or (name != "debounce" and not value)
            ):
                raise ValueError(
                    f"{name} must be finite and positive (debounce may be 0)"
                )
        if self.max_debounce < self.debounce:
            raise ValueError("max_debounce must be at least debounce")


@dataclass(frozen=True)
class WatchResult:
    exit_code: int
    indexed_runs: int
    failed_runs: int
    pending: bool = False
    last_error: str | None = None


@dataclass
class _Schedule:
    """Pure monotonic-time controller, deliberately independent of sleeping."""

    options: WatchOptions
    observed: dict | None = None
    pending_since: float | None = None
    changed_at: float = 0.0
    retry_at: float | None = None

    def observe(self, snapshot: dict, now: float) -> bool:
        if snapshot == self.observed:
            return False
        self.observed = snapshot
        self.mark(now)
        return True

    def mark(self, now: float) -> None:
        if self.pending_since is None:
            self.pending_since = now
        self.changed_at = now

    def due(self, now: float) -> bool:
        if self.pending_since is None:
            return False
        if self.retry_at is not None:
            return now >= self.retry_at
        return now >= min(
            self.changed_at + self.options.debounce,
            self.pending_since + self.options.max_debounce,
        )

    def failed(self, now: float) -> None:
        self.mark(now)
        self.retry_at = now + self.options.retry_interval

    def complete(self) -> None:
        self.pending_since = self.retry_at = None


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _identity(root: Path) -> dict:
    if (
        not root.is_dir()
        or Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root
    ):
        raise ValueError("watch requires the Git worktree root")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    common = (root / common).resolve()
    ref_result = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if ref_result.returncode not in (0, 1):
        raise OSError("cannot read Git HEAD reference")
    ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None
    try:
        head = _git(root, "rev-parse", "--verify", "HEAD")
    except subprocess.CalledProcessError:
        # A symbolic HEAD pointing at an absent branch is an unborn checkout.
        if ref is None:
            raise
        check = subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if check.returncode != 1:
            raise
        head = None
    return {
        "root": str(root),
        "git_dir": str(git_dir),
        "common_dir": str(common),
        "ref": ref,
        "detached": ref is None,
        "head": head,
    }


def _scan(root: Path, options: IndexOptions) -> dict:
    identity = _identity(root)
    # The public discovery API falls back to os.walk on Git errors. A watcher
    # must not mistake that partial listing for authoritative deletion, so use
    # checked Git enumeration and the *same* candidate/filter builder instead.
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    paths = []
    for raw in sorted(set(result.stdout.split(b"\0")) - {b""}):
        path = root / os.fsdecode(raw)
        try:
            path.lstat()
        except FileNotFoundError:
            continue  # Deleted tracked files are still listed by ls-files.
        paths.append(path)
    discovery = _build_discovery(root, paths, options, mode="git")
    if discovery.skipped.get("stat_error"):
        raise OSError("incomplete file discovery")
    manifest = hashlib.sha256()
    for candidate in sorted(discovery.candidates, key=lambda item: item.relative_path):
        stat = candidate.absolute_path.stat()
        manifest.update(
            json.dumps(
                [
                    candidate.relative_path,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                ],
                ensure_ascii=True,
            ).encode()
        )
    if _identity(root) != identity:
        raise OSError("Git identity changed during discovery")
    return {
        "identity": identity,
        "manifest": manifest.hexdigest(),
        "file_count": len(discovery.candidates),
    }


def _check_state_file(path: Path, *, fd: int | None = None) -> os.stat_result | None:
    try:
        info = path.lstat() if fd is None else os.fstat(fd)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(
            f"Unsafe watch state file (must be regular, singly linked, not a symlink): {path}"
        )
    return info


def _load_state(path: Path, expected: dict) -> dict:
    if _check_state_file(path) is None:
        return {}
    # Nonblocking open plus fstat prevents a FIFO swapped in after lstat from
    # hanging the watcher. Directory-renaming attacks are outside this contract.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    try:
        info = _check_state_file(path, fd=fd)
        assert info is not None
        if info.st_size > 32_768:
            return {}
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            try:
                raw = stream.read(32_769)
            except UnicodeError:
                return {}
    finally:
        os.close(fd)
    if len(raw) > 32_768:
        return {}
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("version") != 1:
            return {}
        if any(state.get(key) != value for key, value in expected.items()):
            return {}
        # Do not propagate arbitrary/corrupt JSON or unbounded fields from an
        # old status file. History is advisory: startup always reconciles.
        snapshot = state.get("last_successful_snapshot")
        successful_at = state.get("last_success_at")
        if not isinstance(snapshot, dict) or not isinstance(
            successful_at, (int, float)
        ):
            return {}
        if not math.isfinite(successful_at) or not 0 <= successful_at <= time.time():
            return {}
        identity = snapshot.get("identity")
        if not isinstance(identity, dict) or any(
            identity.get(key) != value for key, value in expected["worktree"].items()
        ):
            return {}
        if set(identity) != {
            "root",
            "git_dir",
            "common_dir",
            "ref",
            "head",
            "detached",
        }:
            return {}
        ref, head = identity.get("ref"), identity.get("head")
        if ref is not None and (
            not isinstance(ref, str) or not ref.startswith("refs/") or len(ref) > 1024
        ):
            return {}
        if head is not None and (
            not isinstance(head, str)
            or len(head) not in (40, 64)
            or any(c not in "0123456789abcdef" for c in head)
        ):
            return {}
        manifest, count = snapshot.get("manifest"), snapshot.get("file_count")
        if (
            not isinstance(manifest, str)
            or len(manifest) != 64
            or any(c not in "0123456789abcdef" for c in manifest)
            or not isinstance(count, int)
            or count < 0
            or identity.get("detached") is not (ref is None)
        ):
            return {}
        return {
            "last_successful_snapshot": {
                "identity": identity,
                "manifest": manifest,
                "file_count": count,
            },
            "last_success_at": successful_at,
        }
    except (FileNotFoundError, ValueError, UnicodeError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    _check_state_file(path)
    fd, temporary = tempfile.mkstemp(prefix=".watch-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
        _check_state_file(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def watch_project(
    root_path: str | Path,
    *,
    project_slug: str,
    repo_slug: str,
    options: WatchOptions | None = None,
    index_options: IndexOptions | None = None,
    once: bool = False,
    on_event: Callable[[dict], None] | None = None,
    stop_event: threading.Event | None = None,
) -> WatchResult:
    """Reconcile immediately, then poll until stopped (or return after --once).

    Invalid identity/registration and duplicate watchers raise before indexing.
    Runtime scan/index failures are persisted and retried, including index-lock
    contention. Event callbacks run serially; observer errors cannot break work.
    """
    root = Path(root_path).expanduser().resolve(strict=True)
    opts = options or WatchOptions()
    index_opts = index_options or IndexOptions()
    identity = _identity(root)
    resolved = resolve_repo(project=project_slug, repo=repo_slug, cwd=root)
    repo = resolved.get("repo")
    if not repo or Path(repo["root_path"]).expanduser().resolve() != root:
        raise ValueError("registered repository root does not match watched worktree")
    # Use resolved canonical slugs, never aliases, in persisted identity and calls.
    project_slug = resolved["project"]["slug"]
    repo_slug = repo["slug"]
    stopped = stop_event or threading.Event()
    indexed = failed = 0
    last_error = None
    schedule = _Schedule(opts)
    expected = {
        "project_slug": project_slug,
        "repo_slug": repo_slug,
        "root": str(root),
        "worktree": {key: identity[key] for key in ("root", "git_dir", "common_dir")},
    }

    def emit(event: str, **details) -> None:
        if on_event:
            try:
                on_event(
                    deepcopy(
                        {
                            "event": event,
                            "project_slug": project_slug,
                            "repo_slug": repo_slug,
                            **details,
                        }
                    )
                )
            except Exception:
                pass  # An optional status consumer must not stop reconciliation.

    with repository_lock(root, name="watch"):
        state_path = root / ".gpc" / "watch-state.json"
        previous = _load_state(state_path, expected)
        state = {
            "version": 1,
            **expected,
            "last_successful_snapshot": previous.get("last_successful_snapshot"),
            "last_success_at": previous.get("last_success_at"),
            "pending": True,
            "error": None,
        }

        def persist() -> None:
            state["updated_at"] = time.time()
            successful_at = state.get("last_success_at")
            state["freshness_seconds"] = (
                max(0.0, state["updated_at"] - successful_at)
                if isinstance(successful_at, (int, float))
                else None
            )
            state["pending"] = schedule.pending_since is not None
            try:
                _write_state(state_path, state)
            except OSError:
                # A temporarily absent/unmounted root cannot store status. Keep
                # the pending controller in memory and retry, never index an
                # empty replacement scan. The previous on-disk state is safe.
                emit("state_error", error="state_unavailable")

        # Always reconcile on startup, including a previously successful state:
        # offline edits and vector loss are only repaired by the real indexer.
        startup = True
        schedule.mark(time.monotonic())
        while True:
            if stopped.is_set() and not once:
                persist()
                break
            try:
                before = _scan(root, index_opts)
                if schedule.observe(before, time.monotonic()):
                    state["observed_snapshot"] = before
                    persist()
                    emit("pending", snapshot=before)
                if startup or schedule.due(time.monotonic()):
                    startup = False
                    persist()  # Crash during indexing leaves a pending marker.
                    emit("indexing", snapshot=before)
                    stats = index_project_path(
                        root, slug=project_slug, repo_slug=repo_slug, options=index_opts
                    )
                    if stats.files_failed:
                        raise _IndexFailed()
                    indexed += 1
                    after = _scan(root, index_opts)
                    if after == before:
                        schedule.complete()
                        state["last_successful_snapshot"] = after
                        state["last_success_at"] = time.time()
                    else:
                        schedule.complete()
                        schedule.observe(after, time.monotonic())
                        state["observed_snapshot"] = after
                    last_error = state["error"] = None
                    persist()
                    emit("indexed", snapshot=before, pending=state["pending"])
            except Exception as exc:
                failed += 1
                # Store a bounded category, never source-bearing exception text.
                last_error = (
                    "repository_busy"
                    if isinstance(exc, RepositoryBusyError)
                    else type(exc).__name__
                )
                state["error"] = last_error
                schedule.failed(time.monotonic())
                startup = False
                persist()
                emit("error", error=last_error)
            if once:
                break
            stopped.wait(opts.poll_interval)
        pending = schedule.pending_since is not None
        return WatchResult(
            1 if last_error or (once and pending) else 0,
            indexed,
            failed,
            pending,
            last_error,
        )


class _IndexFailed(Exception):
    """At least one source file failed; auxiliary warnings alone are harmless."""
