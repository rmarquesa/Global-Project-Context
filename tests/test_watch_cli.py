from __future__ import annotations

import builtins
import json
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gpc import cli, watcher
from gpc import indexer
from gpc.index_lock import RepositoryBusyError, repository_lock
from gpc.registry import ProjectResolutionError


@pytest.fixture
def registered(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = {"slug": "project", "id": "project-id"}
    repo = {
        "slug": "repo",
        "project_slug": "project",
        "root_path": str(tmp_path),
    }
    resolve = Mock(return_value={"project": project, "repo": repo})
    monkeypatch.setattr(cli, "resolve_repo", resolve)
    monkeypatch.setattr(cli, "list_repos", Mock(return_value=[repo]))
    return tmp_path, project, repo, resolve


def test_watch_parser_defaults_and_help_need_no_watcher_import(monkeypatch, capsys):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "gpc.watcher":
            raise AssertionError("parser imported watcher")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    args = cli.build_parser().parse_args(["watch"])
    assert args.path == "."
    assert args.project is None and args.repo is None
    assert (args.interval, args.debounce, args.max_debounce, args.retry_interval) == (
        1.0,
        1.0,
        10.0,
        5.0,
    )
    assert not args.once and not args.json
    assert args.func is cli.cmd_watch
    with pytest.raises(SystemExit) as exc:
        cli.main(["watch", "--help"])
    assert exc.value.code == 0
    assert "foreground" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--interval", "0"],
        ["--interval", "-1"],
        ["--interval", "nan"],
        ["--debounce", "-1"],
        ["--max-debounce", "0"],
        ["--retry-interval", "inf"],
        ["--interval", "no"],
        ["--reset"],
        ["--force"],
    ],
)
def test_watch_rejects_invalid_and_destructive_arguments(arguments):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["watch", *arguments])
    assert exc.value.code == 2


def test_watch_dispatch_options_json_and_once_result(registered, monkeypatch, capsys):
    root, project, repo, resolve = registered

    def watch(path, **kwargs):
        assert path == root
        assert kwargs["project_slug"] == project["slug"]
        assert kwargs["repo_slug"] == repo["slug"]
        assert kwargs["once"] is True
        assert kwargs["options"] == watcher.WatchOptions(0.2, 0, 2.5, 0.5)
        assert kwargs["index_options"] == cli.IndexOptions()
        assert not kwargs["stop_event"].is_set()
        kwargs["on_event"]({"event": "indexed", "pending": False})
        return watcher.WatchResult(exit_code=7, indexed_runs=1, failed_runs=2)

    run = Mock(side_effect=watch)
    monkeypatch.setattr(watcher, "watch_project", run)
    assert (
        cli.main(
            [
                "watch",
                str(root),
                "--project",
                "alias",
                "--repo",
                "repo",
                "--once",
                "--json",
                "--interval",
                "0.2",
                "--debounce",
                "0",
                "--max-debounce",
                "2.5",
                "--retry-interval",
                "0.5",
            ]
        )
        == 7
    )
    resolve.assert_called_once_with(cwd=root, project="alias", repo="repo")
    run.assert_called_once()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {"event": "indexed", "pending": False},
        {
            "event": "stopped",
            "indexed_runs": 1,
            "failed_runs": 2,
            "exit_code": 7,
            "pending": False,
            "error": None,
        },
    ]


def test_watch_summary_preserves_incomplete_reconciliation(
    registered, monkeypatch, capsys
):
    root, *_ = registered
    monkeypatch.setattr(
        watcher,
        "watch_project",
        Mock(return_value=watcher.WatchResult(1, 1, 0, pending=True)),
    )
    assert cli.main(["watch", str(root), "--once", "--json"]) == 1
    event = json.loads(capsys.readouterr().out)
    assert event["pending"] is True
    assert event["failed_runs"] == 0
    assert event["error"] is None


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git"]),
        subprocess.TimeoutExpired(["git"], 30),
    ],
)
def test_watch_git_startup_failure_has_no_traceback(
    registered, monkeypatch, capsys, error
):
    root, *_ = registered
    monkeypatch.setattr(watcher, "watch_project", Mock(side_effect=error))
    assert cli.main(["watch", str(root), "--once"]) == 1
    captured = capsys.readouterr()
    assert "gpc watch:" in captured.err
    assert "Traceback" not in captured.err


def test_watch_explicit_project_without_repo_uses_exact_registered_root(
    registered,
    monkeypatch,
):
    root, project, repo, resolve = registered
    resolve.return_value = {"project": project, "repo": None}
    other = {**repo, "slug": "other", "root_path": str(root / "other")}
    cli.list_repos.return_value = [other, repo]
    run = Mock(return_value=watcher.WatchResult(0, 1, 0))
    monkeypatch.setattr(watcher, "watch_project", run)
    assert cli.main(["watch", str(root), "--project", "alias", "--once"]) == 0
    cli.list_repos.assert_called_once_with("project")
    assert run.call_args.kwargs["repo_slug"] == "repo"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_set_stop_event_and_handlers_are_restored(
    registered, monkeypatch, signum
):
    root, *_ = registered
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def watch(path, **kwargs):
        signal.getsignal(signum)(signum, None)
        assert kwargs["stop_event"].is_set()
        return watcher.WatchResult(0, 0, 0)

    monkeypatch.setattr(watcher, "watch_project", watch)
    assert cli.main(["watch", str(root)]) == 0
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


@pytest.mark.parametrize(
    "error", [ValueError("bad identity"), RepositoryBusyError("busy")]
)
def test_expected_errors_restore_handlers_and_emit_clean_json(
    registered,
    monkeypatch,
    capsys,
    error,
):
    root, *_ = registered
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(watcher, "watch_project", Mock(side_effect=error))
    assert cli.main(["watch", str(root), "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"event": "error", "error": str(error)}
    assert captured.err == ""
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def test_unexpected_failure_also_restores_handlers(registered, monkeypatch):
    root, *_ = registered
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(
        watcher, "watch_project", Mock(side_effect=RuntimeError("unexpected"))
    )
    with pytest.raises(RuntimeError, match="unexpected"):
        cli.main(["watch", str(root)])
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


@pytest.mark.parametrize(
    "case", ["repo_only", "max_delay", "wrong_root", "no_repo", "unknown"]
)
def test_watch_validation_and_registration_errors_are_actionable(
    registered,
    monkeypatch,
    capsys,
    case,
):
    root, project, repo, resolve = registered
    arguments = []
    if case == "repo_only":
        arguments = ["--repo", "repo"]
        expected = "--repo requires --project"
    elif case == "max_delay":
        arguments = ["--debounce", "5", "--max-debounce", "1"]
        expected = "max_debounce"
    elif case == "wrong_root":
        resolve.return_value = {
            "project": project,
            "repo": {**repo, "root_path": str(root.parent)},
        }
        expected = "exact root"
    elif case == "no_repo":
        resolve.return_value = {"project": project, "repo": None}
        cli.list_repos.return_value = []
        expected = "gpc init"
    else:
        resolve.side_effect = ProjectResolutionError("Unknown project; register first")
        expected = "register"
    run = Mock()
    monkeypatch.setattr(watcher, "watch_project", run)
    assert cli.main(["watch", str(root), *arguments]) == 1
    assert expected in capsys.readouterr().err
    run.assert_not_called()


def test_watch_rejects_subdirectory_before_resolution(registered, monkeypatch, capsys):
    root, _, _, resolve = registered
    subdir = root / "subdir"
    subdir.mkdir()
    run = Mock()
    monkeypatch.setattr(watcher, "watch_project", run)
    assert cli.main(["watch", str(subdir)]) == 1
    assert f"gpc watch {root}" in capsys.readouterr().err
    resolve.assert_not_called()
    run.assert_not_called()


def test_watch_rejects_non_git_and_missing_paths(tmp_path, monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(watcher, "watch_project", run)
    assert cli.main(["watch", str(tmp_path)]) == 1
    assert "Git worktree root" in capsys.readouterr().err
    assert cli.main(["watch", str(tmp_path / "missing")]) == 1
    assert "missing" in capsys.readouterr().err
    run.assert_not_called()


def test_manual_index_busy_returns_nonzero_without_traceback(tmp_path, capsys):
    with repository_lock(tmp_path):
        assert cli.main(["index", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "busy" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("files_failed", [0, 1])
def test_cli_once_real_loop_and_public_indexer_lock(
    registered,
    monkeypatch,
    capsys,
    files_failed,
):
    root, project, repo, _ = registered
    (root / "README.md").write_text("Local test repository\n")
    monkeypatch.setattr(
        watcher, "resolve_repo", Mock(return_value={"project": project, "repo": repo})
    )

    def implementation(path, **kwargs):
        assert path == root
        assert kwargs["slug"] == "project" and kwargs["repo_slug"] == "repo"
        # Both lifetime watch and per-run index locks are held, without double acquisition.
        for name in ("index", "watch"):
            with pytest.raises(RepositoryBusyError):
                with repository_lock(root, name=name):
                    pass
        return SimpleNamespace(files_failed=files_failed)

    run = Mock(side_effect=implementation)
    monkeypatch.setattr(indexer, "_index_project_path", run)
    assert cli.main(["watch", str(root), "--once", "--json"]) == files_failed
    run.assert_called_once()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["exit_code"] == files_failed
    assert events[-1]["failed_runs"] == files_failed
    assert events[-1]["indexed_runs"] == 1 - files_failed
    for name in ("index", "watch"):
        with repository_lock(root, name=name):
            pass


def test_linked_worktree_requires_own_registered_root_and_has_independent_lock(
    registered,
    monkeypatch,
    capsys,
):
    root, project, repo, resolve = registered
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "test",
        ],
        check=True,
    )
    linked = root / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-qb", "linked", str(linked)],
        check=True,
    )
    run = Mock(return_value=watcher.WatchResult(0, 1, 0))
    monkeypatch.setattr(watcher, "watch_project", run)
    # A project config or explicit old slug must never reassign the main root.
    assert (
        cli.main(
            [
                "watch",
                str(linked),
                "--project",
                "project",
                "--repo",
                "repo",
                "--once",
            ]
        )
        == 1
    )
    assert "distinct repo slug" in capsys.readouterr().err
    run.assert_not_called()
    linked_repo = {**repo, "slug": "linked", "root_path": str(linked)}
    resolve.return_value = {"project": project, "repo": linked_repo}
    with repository_lock(root):
        with repository_lock(linked):
            assert (
                cli.main(
                    [
                        "watch",
                        str(linked),
                        "--project",
                        "project",
                        "--repo",
                        "linked",
                        "--once",
                    ]
                )
                == 0
            )
    assert run.call_args.kwargs["repo_slug"] == "linked"
