from __future__ import annotations

from pathlib import Path

import pytest

from gpc.verify import (
    VerificationCheck,
    VerificationReport,
    summarize_checks,
    verify_graphify_report_file,
    verify_index_state,
)


def test_summarize_checks_counts_statuses_and_sets_overall_pass() -> None:
    checks = [
        VerificationCheck("postgres", "pass", "reachable"),
        VerificationCheck(
            "graph", "warn", "not projected", remediation="run gpc graph-sync"
        ),
        VerificationCheck("mcp", "skip", "quick mode"),
    ]

    report = VerificationReport(project_slug="gpc", checks=checks)

    assert report.summary == {"pass": 1, "warn": 1, "fail": 0, "skip": 1}
    assert report.overall_status == "warn"
    assert report.to_dict()["checks"][1]["remediation"] == "run gpc graph-sync"


def test_summarize_checks_prefers_fail_over_warn() -> None:
    result = summarize_checks(
        [
            VerificationCheck("postgres", "warn", "slow"),
            VerificationCheck("qdrant", "fail", "unreachable"),
        ]
    )

    assert result["overall_status"] == "fail"


def test_graphify_report_file_check_warns_when_graphify_dir_exists_without_report(
    tmp_path: Path,
) -> None:
    (tmp_path / "graphify-out").mkdir()

    check = verify_graphify_report_file(tmp_path)

    assert check.status == "warn"
    assert check.name == "graphify_report"
    assert "graphify update" in check.remediation


def test_graphify_report_file_check_skips_when_graphify_dir_absent(
    tmp_path: Path,
) -> None:
    check = verify_graphify_report_file(tmp_path)

    assert check.status == "skip"
    assert check.name == "graphify_report"


def test_index_state_warns_on_vector_count_deficit_and_excess(monkeypatch) -> None:
    from gpc import verify

    monkeypatch.setattr(
        verify,
        "get_index_status",
        lambda **kwargs: {
            "runs": [{"status": "succeeded"}],
            "files": 1,
            "chunks": 4,
            "qdrant_points": 3,
        },
    )
    deficit = verify_index_state()
    assert deficit.status == "warn"
    assert "missing" in deficit.message
    assert "identity" in deficit.message

    monkeypatch.setattr(
        verify,
        "get_index_status",
        lambda **kwargs: {
            "runs": [{"status": "succeeded"}],
            "files": 1,
            "chunks": 4,
            "qdrant_points": 5,
        },
    )
    assert "excess" in verify_index_state().message


@pytest.mark.parametrize(
    "runs,files,chunks,points,expected,message",
    [
        ([{"status": "succeeded"}], 100, 1000, 1000, "pass", "populated"),
        ([], 100, 1000, 1, "warn", "No index runs"),
        ([{"status": "failed"}], 100, 1000, 1, "fail", "Latest index run"),
        ([{"status": "succeeded"}], 0, 0, 0, "warn", "zero"),
        ([{"status": "succeeded"}], 1, 2, 0, "warn", "zero"),
        ([{"status": "succeeded"}], 0, 2, 2, "warn", "zero"),
        ([{"status": "succeeded"}], 1, 0, 2, "warn", "zero"),
        ([{"status": "succeeded"}], 100, 1000, 1, "warn", "999 missing"),
    ],
)
def test_index_state_preserves_populated_and_run_state_checks(
    monkeypatch, runs, files, chunks, points, expected, message
) -> None:
    from gpc import verify

    calls = []

    def get_status(**kwargs):
        calls.append(kwargs)
        return {"runs": runs, "files": files, "chunks": chunks, "qdrant_points": points}

    monkeypatch.setattr(verify, "get_index_status", get_status)
    check = verify_index_state(project="logical", cwd="/repo")

    assert calls == [{"project": "logical", "cwd": "/repo", "runs": 1}]
    assert check.status == expected
    assert message in check.message
