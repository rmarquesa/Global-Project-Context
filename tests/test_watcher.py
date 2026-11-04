from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import threading
from types import SimpleNamespace

import pytest

from gpc import watcher
from gpc.index_lock import RepositoryBusyError, repository_lock
from gpc.indexer import DiscoveryResult, IndexOptions


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Watcher Test")
    (root / "main.py").write_text("answer = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root.resolve()


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    def resolve(*, project, repo, cwd):
        return {
            "project": {"slug": project},
            "repo": {"slug": repo, "root_path": str(cwd)},
        }

    monkeypatch.setattr(watcher, "resolve_repo", resolve)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000 + self.now


class Stop:
    """Event-compatible virtual polling clock: no real sleep anywhere."""

    def __init__(self, clock, ticks=10, on_tick=None):
        self.clock = clock
        self.ticks = ticks
        self.on_tick = on_tick
        self.count = 0
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.clock.now += delay
        self.count += 1
        if self.on_tick:
            self.on_tick(self.count)
        if self.count >= self.ticks:
            self.set()
        return self.stopped


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(watcher.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(watcher.time, "time", clock.time)
    return clock


def record_index(monkeypatch, action=None):
    calls = []

    def index(root, **kwargs):
        calls.append((root, kwargs))
        if action:
            result = action(len(calls))
            if result is not None:
                return result
        return SimpleNamespace(files_failed=0, errors=[])

    monkeypatch.setattr(watcher, "index_project_path", index)
    return calls


def watch(root, **kwargs):
    return watcher.watch_project(
        root, project_slug="project", repo_slug="repo", **kwargs
    )


def state(root):
    return json.loads((root / ".gpc" / "watch-state.json").read_text())


@pytest.mark.parametrize(
    "field", ["poll_interval", "debounce", "max_debounce", "retry_interval"]
)
@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_invalid_delays(field, value):
    with pytest.raises(ValueError):
        watcher.WatchOptions(**{field: value})


@pytest.mark.parametrize("field", ["poll_interval", "max_debounce", "retry_interval"])
def test_positive_delays(field):
    with pytest.raises(ValueError):
        watcher.WatchOptions(**{field: 0})


def test_options_frozen_and_maximum_validated():
    with pytest.raises(ValueError):
        watcher.WatchOptions(debounce=3, max_debounce=2)
    options = watcher.WatchOptions(debounce=0)
    with pytest.raises(FrozenInstanceError):
        options.debounce = 2


def test_once_immediate_and_incremental_options(root, monkeypatch, clock):
    calls = record_index(monkeypatch)
    result = watch(root, once=True)
    assert result == watcher.WatchResult(0, 1, 0)
    assert calls == [
        (root, {"slug": "project", "repo_slug": "repo", "options": IndexOptions()})
    ]
    persisted = state(root)
    assert not persisted["pending"]
    assert persisted["last_successful_snapshot"]["identity"]["head"] == git(
        root, "rev-parse", "HEAD"
    )
    assert persisted["freshness_seconds"] == 0
    assert "answer =" not in json.dumps(persisted)
    assert len(json.dumps(persisted)) < 3000


def test_custom_index_options_forwarded_unchanged(root, monkeypatch):
    calls = record_index(monkeypatch)
    opts = replace(IndexOptions(), include_unknown_text=True)
    watch(root, once=True, index_options=opts)
    assert calls[0][1]["options"] is opts


@pytest.mark.parametrize("operation", ["create", "update", "delete", "rename"])
def test_files_schedule_full_incremental_reconciliation(
    root, monkeypatch, clock, operation
):
    calls = record_index(monkeypatch)

    def edit(tick):
        if tick != 1:
            return
        path = root / "main.py"
        if operation == "create":
            (root / "new.py").write_text("new = True\n")
        elif operation == "update":
            path.write_text("answer = 1234\n")
        elif operation == "delete":
            path.unlink()
        else:
            path.rename(root / "renamed.py")

    result = watch(root, stop_event=Stop(clock, 5, edit))
    assert result.indexed_runs == 2
    assert len(calls) == 2
    assert all(call[1]["options"].prune_deleted for call in calls)
    assert not state(root)["pending"]


def test_burst_coalesces(root, monkeypatch, clock):
    times = []
    record_index(monkeypatch, lambda _: times.append(clock.now))

    def edits(tick):
        if tick in (1, 2, 3):
            (root / "main.py").write_text(f"answer = {tick}\n")

    result = watch(
        root, options=watcher.WatchOptions(debounce=2), stop_event=Stop(clock, 9, edits)
    )
    assert result.indexed_runs == 2
    assert times == [0, 5]


def test_continuous_changes_flush_by_maximum(root, monkeypatch, clock):
    times = []
    record_index(monkeypatch, lambda _: times.append(clock.now))

    def edits(tick):
        (root / "main.py").write_text(f"answer = {tick}\n")

    result = watch(
        root,
        options=watcher.WatchOptions(debounce=3, max_debounce=4),
        stop_event=Stop(clock, 12, edits),
    )
    assert result.indexed_runs == 3
    assert times == [0, 5, 10]
    assert state(root)["pending"]


def test_unchanged_and_ignored_writes_do_not_repeat(root, monkeypatch, clock):
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    (root / "graphify-out").mkdir()
    calls = record_index(monkeypatch)

    def writes(tick):
        (root / ".gpc" / "metrics.json").write_text(str(tick))
        (root / "generated" / "output.py").write_text(str(tick))
        (root / "graphify-out" / "output.py").write_text(str(tick))

    result = watch(root, stop_event=Stop(clock, 7, writes))
    assert result.indexed_runs == len(calls) == 1


@pytest.mark.parametrize("kind", ["exception", "files_failed", "busy"])
def test_failed_run_retries_without_edit(root, monkeypatch, clock, kind):
    times = []

    def action(number):
        times.append(clock.now)
        if number == 1:
            if kind == "files_failed":
                return SimpleNamespace(files_failed=1, errors=["file failed"])
            if kind == "busy":
                # Exercise the real shared lock instead of inventing an error.
                with repository_lock(root, name="index"):
                    with repository_lock(root, name="index"):
                        pass
            raise RuntimeError("temporary")

    calls = record_index(monkeypatch, action)
    result = watch(
        root, options=watcher.WatchOptions(retry_interval=3), stop_event=Stop(clock, 7)
    )
    assert (result.indexed_runs, result.failed_runs, result.exit_code) == (1, 1, 0)
    assert len(calls) == 2
    assert times == [0, 3]
    assert not state(root)["pending"]


@pytest.mark.parametrize("failure", [False, True])
def test_once_exit_status_and_pending_failure(root, monkeypatch, failure):
    record_index(
        monkeypatch,
        lambda _: SimpleNamespace(
            files_failed=int(failure), errors=["self_metrics: optional warning"]
        ),
    )
    result = watch(root, once=True)
    assert result.exit_code == int(failure)
    assert result.failed_runs == int(failure)
    assert result.indexed_runs == int(not failure)
    assert result.pending == failure
    assert state(root)["pending"] == failure


def test_once_raised_error_nonzero(root, monkeypatch):
    def fail(_):
        raise RuntimeError("source contents must not enter status")

    record_index(monkeypatch, fail)
    result = watch(root, once=True)
    assert (result.exit_code, result.indexed_runs, result.failed_runs) == (1, 0, 1)
    assert result.last_error == "RuntimeError"
    assert "source contents" not in json.dumps(state(root))


def test_edit_during_index_is_followed_up_not_marked_fresh(root, monkeypatch, clock):
    events = []

    def action(number):
        if number == 1:
            (root / "main.py").write_text("changed during index\n")

    calls = record_index(monkeypatch, action)

    def event(event):
        if event["event"] == "indexed":
            events.append((event["pending"], state(root)["last_successful_snapshot"]))

    result = watch(root, stop_event=Stop(clock, 5), on_event=event)
    assert result.indexed_runs == len(calls) == 2
    assert events[0] == (True, None)
    assert events[1][0] is False
    assert state(root)["last_successful_snapshot"] == watcher._scan(
        root, IndexOptions()
    )


def test_once_edit_during_index_reports_pending(root, monkeypatch):
    record_index(
        monkeypatch,
        lambda _: (root / "main.py").write_text("newer checkout\n") and None,
    )
    result = watch(root, once=True)
    assert result.indexed_runs == 1
    assert result.pending
    assert result.exit_code != 0
    assert result.last_error is None
    assert state(root)["last_successful_snapshot"] is None


@pytest.mark.parametrize(
    "previous",
    [
        "corrupt",
        "failed",
        "pending",
        "success",
        "wrong_root",
        "wrong_repo",
        "wrong_version",
    ],
)
def test_every_restart_reconciles(root, monkeypatch, previous):
    calls = record_index(monkeypatch)
    watch(root, once=True)
    path = root / ".gpc" / "watch-state.json"
    if previous == "corrupt":
        path.write_text("not JSON")
    else:
        value = state(root)
        if previous == "failed":
            value.update(error="RuntimeError", pending=True)
        elif previous == "pending":
            value["pending"] = True
        elif previous == "wrong_root":
            value["root"] = "/wrong"
        elif previous == "wrong_repo":
            value["repo_slug"] = "another-repo"
        elif previous == "wrong_version":
            value["version"] = -1
        path.write_text(json.dumps(value))
    result = watch(root, once=True)
    assert len(calls) == 2
    assert result.exit_code == 0
    assert not state(root)["pending"]


def test_symlink_state_rejected_without_overwriting_target(root, monkeypatch, tmp_path):
    calls = record_index(monkeypatch)
    target = tmp_path / "target"
    target.write_text("do not touch")
    (root / ".gpc").mkdir()
    (root / ".gpc" / "watch-state.json").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        watch(root, once=True)
    assert calls == []
    assert target.read_text() == "do not touch"


def test_corrupt_state_json_types_recover(root, monkeypatch):
    record_index(monkeypatch)
    (root / ".gpc").mkdir()
    (root / ".gpc" / "watch-state.json").write_text("[]")
    assert watch(root, once=True).exit_code == 0


def test_wrong_registered_root_rejected(root, monkeypatch, tmp_path):
    calls = record_index(monkeypatch)
    monkeypatch.setattr(
        watcher,
        "resolve_repo",
        lambda **_: {
            "project": {"slug": "project"},
            "repo": {"slug": "repo", "root_path": str(tmp_path)},
        },
    )
    with pytest.raises(ValueError, match="registered repository root"):
        watch(root, once=True)
    assert calls == []
    assert not (root / ".gpc" / "watch-state.json").exists()


def test_missing_registration_rejected(root, monkeypatch):
    calls = record_index(monkeypatch)
    monkeypatch.setattr(
        watcher,
        "resolve_repo",
        lambda **_: {"project": {"slug": "project"}, "repo": None},
    )
    with pytest.raises(ValueError, match="registered repository root"):
        watch(root, once=True)
    assert not calls


def test_canonical_registered_slugs_used(root, monkeypatch):
    calls = record_index(monkeypatch)
    monkeypatch.setattr(
        watcher,
        "resolve_repo",
        lambda **_: {
            "project": {"slug": "canonical-project"},
            "repo": {"slug": "canonical-repo", "root_path": str(root)},
        },
    )
    watch(root, once=True)
    assert calls[0][1]["slug"] == "canonical-project"
    assert state(root)["repo_slug"] == "canonical-repo"


def test_non_git_and_subfolder_rejected(root, monkeypatch, tmp_path):
    calls = record_index(monkeypatch)
    with pytest.raises(subprocess.CalledProcessError):
        watch(tmp_path, once=True)
    subfolder = root / "child"
    subfolder.mkdir()
    with pytest.raises(ValueError, match="worktree root"):
        watch(subfolder, once=True)
    assert calls == []


@pytest.mark.parametrize("change", ["branch", "commit", "detached"])
def test_git_identity_change_schedules_without_stat_changes(
    root, monkeypatch, clock, change
):
    before = watcher._scan(root, IndexOptions())
    calls = record_index(monkeypatch)

    def alter(tick):
        if tick == 1:
            if change == "branch":
                git(root, "switch", "-qc", "other")
            elif change == "commit":
                git(root, "commit", "--allow-empty", "-qm", "empty commit")
            else:
                git(root, "checkout", "-q", "--detach")

    result = watch(root, stop_event=Stop(clock, 5, alter))
    after = watcher._scan(root, IndexOptions())
    assert before["manifest"] == after["manifest"]
    assert before["identity"] != after["identity"]
    assert result.indexed_runs == len(calls) == 2
    assert after["identity"]["detached"] == (change == "detached")
    assert state(root)["last_successful_snapshot"]["identity"] == after["identity"]


def test_unborn_worktree_supported(tmp_path, monkeypatch):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-q")
    record_index(monkeypatch)
    assert watch(root, once=True).exit_code == 0
    identity = state(root)["last_successful_snapshot"]["identity"]
    assert identity["head"] is None
    assert identity["ref"].startswith("refs/heads/")
    assert identity["detached"] is False


def test_linked_worktrees_have_distinct_registered_identity_and_state(
    root, tmp_path, monkeypatch
):
    linked = tmp_path / "linked"
    git(root, "worktree", "add", "-qb", "linked", str(linked))
    calls = record_index(monkeypatch)
    registered = {"main": root, "linked": linked.resolve()}

    def resolve(*, project, repo, cwd):
        return {
            "project": {"slug": project},
            "repo": {"slug": repo, "root_path": str(registered[repo])},
        }

    monkeypatch.setattr(watcher, "resolve_repo", resolve)
    for slug, path in registered.items():
        assert (
            watcher.watch_project(
                path, project_slug="project", repo_slug=slug, once=True
            ).exit_code
            == 0
        )
    main_state, linked_state = state(root), state(linked)
    assert main_state["repo_slug"] == "main"
    assert linked_state["repo_slug"] == "linked"
    assert (
        main_state["worktree"]["common_dir"] == linked_state["worktree"]["common_dir"]
    )
    assert main_state["worktree"]["git_dir"] != linked_state["worktree"]["git_dir"]
    assert main_state["root"] != linked_state["root"]
    assert [call[1]["repo_slug"] for call in calls] == ["main", "linked"]
    with pytest.raises(ValueError, match="registered repository root"):
        watcher.watch_project(
            linked, project_slug="project", repo_slug="main", once=True
        )


@pytest.mark.parametrize("failure", ["raised", "stat_error"])
def test_discovery_failure_does_not_index_or_prune_and_recovers(
    root, monkeypatch, clock, failure
):
    calls = record_index(monkeypatch)
    original = watcher._build_discovery
    broken = True

    def discover(*args, **kwargs):
        if broken:
            if failure == "raised":
                raise OSError("transient discovery failure")
            return DiscoveryResult([], Counter(stat_error=1), "git")
        return original(*args, **kwargs)

    def tick(number):
        nonlocal broken
        if number == 2:
            broken = False
        if number < 4:
            assert calls == []

    monkeypatch.setattr(watcher, "_build_discovery", discover)
    result = watch(
        root,
        options=watcher.WatchOptions(retry_interval=3),
        stop_event=Stop(clock, 7, tick),
    )
    assert result.indexed_runs == 1
    assert len(calls) == 1
    assert not state(root)["pending"]


def test_failed_git_enumeration_never_falls_back_to_empty_scan(root, monkeypatch):
    calls = record_index(monkeypatch)
    original = watcher.subprocess.run

    def run(command, **kwargs):
        if "ls-files" in command:
            raise subprocess.CalledProcessError(128, command)
        return original(command, **kwargs)

    monkeypatch.setattr(watcher.subprocess, "run", run)
    result = watch(root, once=True)
    assert result.exit_code == 1
    assert result.pending
    assert not calls


def test_disappearing_repository_not_interpreted_as_deletions(root, monkeypatch, clock):
    moved = root.with_name("temporarily-away")
    calls = record_index(monkeypatch)

    def tick(number):
        if number == 1:
            root.rename(moved)
        if number == 3:
            assert len(calls) == 1
            moved.rename(root)

    result = watch(
        root,
        options=watcher.WatchOptions(retry_interval=2),
        stop_event=Stop(clock, 8, tick),
    )
    assert len(calls) == 2
    assert result.failed_runs >= 1
    assert result.exit_code == 0
    assert not state(root)["pending"]


def test_shutdown_leaves_pending_and_releases_watch_lock(root, monkeypatch, clock):
    calls = record_index(monkeypatch)

    def edit(tick):
        if tick == 1:
            (root / "main.py").write_text("pending offline edit")

    result = watch(root, stop_event=Stop(clock, 2, edit))
    assert result.pending
    assert (
        result.exit_code == 0
    )  # Continuous graceful shutdown may retain pending work.
    assert state(root)["pending"]
    assert len(calls) == 1
    with repository_lock(root, name="watch"):
        pass
    watch(root, once=True)
    assert len(calls) == 2
    assert not state(root)["pending"]


def test_duplicate_watcher_rejected_and_index_lock_not_double_acquired(
    root, monkeypatch
):
    callbacks = []

    def index(_):
        with pytest.raises(RepositoryBusyError):
            watch(root, once=True)
        # The watcher owns only the watch lock; indexer owns this lock itself.
        with repository_lock(root, name="index"):
            callbacks.append("index")

    record_index(monkeypatch, index)
    assert watch(root, once=True).exit_code == 0
    assert callbacks == ["index"]
    with repository_lock(root, name="watch"):
        with pytest.raises(RepositoryBusyError):
            watch(root, once=True)


def test_callbacks_serialized_and_consumer_errors_isolated(root, monkeypatch, clock):
    active = False
    events = []
    calls = record_index(monkeypatch)

    def event(value):
        nonlocal active
        assert not active
        active = True
        events.append(value["event"])
        active = False
        raise RuntimeError("UI consumer failed")

    result = watch(root, stop_event=Stop(clock, 3), on_event=event)
    assert result.exit_code == 0
    assert len(calls) == 1
    assert events == ["pending", "indexing", "indexed"]


def test_real_threading_event_stop_releases_lock_without_index(root, monkeypatch):
    calls = record_index(monkeypatch)
    stopped = threading.Event()
    stopped.set()
    result = watch(root, stop_event=stopped)
    assert result.indexed_runs == 0
    assert result.pending
    assert not calls
    with repository_lock(root, name="watch"):
        pass


def test_once_reconciles_even_when_stop_was_already_set(root, monkeypatch):
    calls = record_index(monkeypatch)
    stopped = threading.Event()
    stopped.set()
    result = watch(root, once=True, stop_event=stopped)
    assert result.indexed_runs == len(calls) == 1


def test_zero_debounce_and_bounded_pending_controller():
    schedule = watcher._Schedule(watcher.WatchOptions(debounce=0))
    assert schedule.observe({"revision": 1}, 0)
    assert schedule.due(0)
    assert not schedule.observe({"revision": 1}, 1)
    schedule.observe({"revision": 2}, 2)
    schedule.observe({"revision": 3}, 3)
    assert schedule.pending_since == 0
    assert schedule.observed == {"revision": 3}
    schedule.complete()
    assert not schedule.due(100)


def test_head_change_during_index_requires_followup(root, monkeypatch, clock):
    first_head = git(root, "rev-parse", "HEAD")

    def action(number):
        if number == 1:
            git(root, "commit", "--allow-empty", "-qm", "during index")

    calls = record_index(monkeypatch, action)
    result = watch(root, stop_event=Stop(clock, 4))
    assert result.indexed_runs == len(calls) == 2
    assert state(root)["last_successful_snapshot"]["identity"]["head"] != first_head


def test_post_index_scan_failure_is_pending_and_retried(root, monkeypatch, clock):
    calls = record_index(monkeypatch)
    scan = watcher._scan
    scans = 0

    def failing_scan(*args):
        nonlocal scans
        scans += 1
        if scans == 2:
            raise OSError("post-index scan unavailable")
        return scan(*args)

    monkeypatch.setattr(watcher, "_scan", failing_scan)
    result = watch(
        root, options=watcher.WatchOptions(retry_interval=2), stop_event=Stop(clock, 5)
    )
    assert len(calls) == 2
    assert result.failed_runs == 1
    assert result.exit_code == 0
    assert not result.pending


@pytest.mark.parametrize("corruption", ["oversized", "timestamp", "snapshot"])
def test_untrusted_history_never_propagates_false_fresh_state(
    root, monkeypatch, corruption
):
    record_index(monkeypatch)
    watch(root, once=True)
    old = state(root)
    if corruption == "oversized":
        old["untrusted"] = "content" * 10_000
    elif corruption == "timestamp":
        old["last_success_at"] = float("inf")
    else:
        old["last_successful_snapshot"] = {"untrusted": "not a snapshot"}
    (root / ".gpc" / "watch-state.json").write_text(json.dumps(old))

    def fail(_):
        raise RuntimeError("temporary")

    record_index(monkeypatch, fail)
    result = watch(root, once=True)
    assert result.exit_code == 1
    persisted = state(root)
    assert persisted["pending"]
    assert persisted["last_successful_snapshot"] is None
    assert persisted["last_success_at"] is None
    assert "untrusted" not in persisted


def test_atomic_state_replace_failure_preserves_previous_file(root, monkeypatch):
    record_index(monkeypatch)
    watch(root, once=True)
    path = root / ".gpc" / "watch-state.json"
    previous = path.read_bytes()

    def fail(*_):
        raise OSError("replace failed")

    monkeypatch.setattr(watcher.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        watcher._write_state(path, {"pending": True})
    assert path.read_bytes() == previous
    assert not list(path.parent.glob(".watch-state-*"))


def test_callback_cannot_mutate_internal_snapshot(root, monkeypatch):
    record_index(monkeypatch)

    def event(value):
        if "snapshot" in value:
            value["snapshot"]["identity"]["head"] = "callback mutation"

    assert watch(root, once=True, on_event=event).exit_code == 0
    assert state(root)["last_successful_snapshot"]["identity"]["head"] == git(
        root, "rev-parse", "HEAD"
    )


def test_once_head_change_during_index_returns_nonzero(root, monkeypatch):
    def action(_):
        git(root, "commit", "--allow-empty", "-qm", "during once index")

    calls = record_index(monkeypatch, action)
    result = watch(root, once=True)
    assert len(calls) == result.indexed_runs == 1
    assert result.exit_code != 0
    assert result.pending
    assert result.last_error is None
    assert result.failed_runs == 0
    assert state(root)["pending"]
    assert state(root)["last_successful_snapshot"] is None


@pytest.fixture(params=["directory", "socket", "fifo", "hardlink"])
def unsafe_state(root, tmp_path, monkeypatch, request):
    metadata = root / ".gpc"
    metadata.mkdir()
    path = metadata / "watch-state.json"
    target = tmp_path / "linked-target"
    target.write_text("unchanged linked target")
    bound_socket = None
    if request.param == "directory":
        path.mkdir()
        (path / "keep").write_text("unchanged directory")
    elif request.param == "socket":
        if not hasattr(socket, "AF_UNIX"):
            pytest.skip("Unix sockets are not supported")
        bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Relative bind avoids macOS's short sockaddr_un path limit.
        monkeypatch.chdir(metadata)
        try:
            bound_socket.bind(path.name)
        except OSError as exc:
            bound_socket.close()
            pytest.skip(f"Unix socket files are not supported: {exc}")
    elif request.param == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are not supported")
        os.mkfifo(path)
    else:
        try:
            os.link(target, path)
        except OSError as exc:
            pytest.skip(f"Hard links are not supported: {exc}")
    try:
        yield path, target
    finally:
        if bound_socket is not None:
            bound_socket.close()


def test_unsafe_state_rejected_before_read_index_or_replacement(
    root, monkeypatch, unsafe_state
):
    path, target = unsafe_state
    original_info = path.lstat()
    calls = record_index(monkeypatch)
    original_open, original_path_open = os.open, Path.open

    def open_file(filename, *args, **kwargs):
        if os.fspath(filename) == str(path):
            pytest.fail("unsafe state must be rejected before os.open")
        return original_open(filename, *args, **kwargs)

    def path_open(filename, *args, **kwargs):
        if filename == path:
            pytest.fail("unsafe state must be rejected before Path.open")
        return original_path_open(filename, *args, **kwargs)

    def no_temporary(*args, **kwargs):
        pytest.fail("unsafe destination must be rejected before creating a temporary")

    # The Path.open guard also makes the old FIFO regression fail immediately,
    # rather than hanging pytest inside read_text waiting for a FIFO writer.
    monkeypatch.setattr(watcher.os, "open", open_file)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(watcher.tempfile, "mkstemp", no_temporary)
    with pytest.raises(ValueError, match="Unsafe watch state file"):
        watch(root, once=True)
    with pytest.raises(ValueError, match="Unsafe watch state file"):
        watcher._write_state(path, {"pending": True})
    assert calls == []
    assert path.lstat() == original_info
    assert target.read_text() == "unchanged linked target"
    if path.is_dir():
        assert (path / "keep").read_text() == "unchanged directory"
    assert not list(path.parent.glob(".watch-state-*"))
    with repository_lock(root, name="watch"):
        pass


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="Requires nonblocking FIFO support",
)
def test_fifo_swapped_before_open_is_nonblocking_rejected_and_fd_closed(
    root, monkeypatch
):
    metadata = root / ".gpc"
    metadata.mkdir()
    path = metadata / "watch-state.json"
    path.write_text("{}")
    calls = record_index(monkeypatch)
    original_open, original_fdopen = os.open, os.fdopen
    opened = []

    def swap_and_open(filename, flags, *args, **kwargs):
        if os.fspath(filename) == str(path):
            assert flags & os.O_NONBLOCK
            assert flags & getattr(os, "O_NOFOLLOW", 0) == getattr(os, "O_NOFOLLOW", 0)
            path.unlink()
            os.mkfifo(path)
            fd = original_open(filename, flags, *args, **kwargs)
            opened.append(fd)
            return fd
        return original_open(filename, flags, *args, **kwargs)

    def no_state_stream(fd, *args, **kwargs):
        if fd in opened:
            pytest.fail("fstat must reject swapped FIFO before creating a read stream")
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(watcher.os, "open", swap_and_open)
    monkeypatch.setattr(watcher.os, "fdopen", no_state_stream)
    with pytest.raises(ValueError, match="Unsafe watch state file"):
        watch(root, once=True)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not calls
    assert stat.S_ISFIFO(path.lstat().st_mode)
    assert not list(metadata.glob(".watch-state-*"))
    with repository_lock(root, name="watch"):
        pass


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Requires FIFO support")
def test_state_destination_rechecked_before_replace_and_temporary_cleaned(
    root, monkeypatch
):
    metadata = root / ".gpc"
    metadata.mkdir()
    path = metadata / "watch-state.json"
    path.write_text("original state")
    original_mkstemp = watcher.tempfile.mkstemp
    opened = []

    def swap_after_temporary(*args, **kwargs):
        fd, temporary = original_mkstemp(*args, **kwargs)
        opened.append(fd)
        path.unlink()
        os.mkfifo(path)
        return fd, temporary

    monkeypatch.setattr(watcher.tempfile, "mkstemp", swap_after_temporary)
    with pytest.raises(ValueError, match="Unsafe watch state file"):
        watcher._write_state(path, {"pending": True})
    assert stat.S_ISFIFO(path.lstat().st_mode)
    assert not list(metadata.glob(".watch-state-*"))
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
