from __future__ import annotations

from pathlib import Path

from gpc.verify import (
    VerificationCheck,
    VerificationReport,
    summarize_checks,
    verify_graphify_report_file,
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
