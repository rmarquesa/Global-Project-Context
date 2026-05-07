from __future__ import annotations

from gpc.maintenance import MaintenanceFinding, build_maintenance_report


def test_build_maintenance_report_counts_findings_and_is_dry_run() -> None:
    report = build_maintenance_report(
        project_slug="gpc",
        findings=[
            MaintenanceFinding(
                "orphan_qdrant_points", "warn", 2, "Qdrant points without chunks"
            ),
            MaintenanceFinding("duplicate_aliases", "fail", 1, "Duplicate aliases"),
        ],
    )

    assert report.project_slug == "gpc"
    assert report.dry_run is True
    assert report.summary == {"warn": 1, "fail": 1, "pass": 0}
    assert report.requires_attention is True


def test_build_maintenance_report_defaults_to_pass() -> None:
    report = build_maintenance_report(project_slug="gpc", findings=[])

    assert report.summary["pass"] == 1
    assert report.requires_attention is False
