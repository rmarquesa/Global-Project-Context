from datetime import date
import subprocess
import sys

import pytest

from gpc.auto_research import generate_proposal


def test_generate_proposal_renders_highest_severity_signal_and_metrics(
    tmp_path,
) -> None:
    result = generate_proposal(
        project_slug="demo-project",
        output_dir=tmp_path,
        generated_on=date(2026, 9, 17),
        signals_loader=lambda **_: [
            {
                "severity": "warning",
                "signal_type": "community_count_spike",
                "message": "Community count rose.",
                "evidence": {"before": 3, "after": 9},
            },
            {
                "severity": "critical",
                "signal_type": "embedding_model_mismatch",
                "message": "Embedding model differs.",
                "evidence": {"expected": "nomic"},
            },
        ],
        snapshots_loader=lambda **_: [
            {
                "created_at": "2026-09-17T12:00:00Z",
                "source": "scheduled",
                "graphify_nodes": 42,
                "community_count": 9,
                "weakly_connected_nodes": 3,
            }
        ],
        graph_summary_loader=lambda *_args, **_kwargs: {
            "found": True,
            "god_nodes": [{"label": "ImportantModule"}],
            "communities": [{"id": 1}],
        },
    )

    assert result.proposal_slug == "embedding-model-mismatch"
    assert result.path.name == "2026-09-17-demo-project-embedding-model-mismatch.md"
    content = result.path.read_text()
    assert "**CRITICAL** `embedding_model_mismatch`" in content
    assert "Community count rose." in content
    assert "| 2026-09-17T12:00:00Z | scheduled | 42 | 9 | 3 |" in content
    assert "Topological focus for review: `ImportantModule`." in content
    assert (
        "never edits source code, creates a pull request, or merges changes" in content
    )


def test_generate_proposal_records_missing_drift_as_baseline(tmp_path) -> None:
    result = generate_proposal(
        project_slug="demo-project",
        output_dir=tmp_path,
        generated_on=date(2026, 9, 17),
        signals_loader=lambda **_: [],
        snapshots_loader=lambda **_: [],
        graph_summary_loader=lambda *_args, **_kwargs: {"found": False},
    )

    assert result.proposal_slug == "metrics-baseline"
    content = result.path.read_text()
    assert "metrics_baseline_incomplete" in content
    assert "No self-metrics snapshots are available" in content


def test_generate_proposal_refuses_to_overwrite_existing_file(tmp_path) -> None:
    kwargs = {
        "project_slug": "demo-project",
        "output_dir": tmp_path,
        "generated_on": date(2026, 9, 17),
        "signals_loader": lambda **_: [],
        "snapshots_loader": lambda **_: [],
        "graph_summary_loader": lambda *_args, **_kwargs: {"found": False},
    }
    generate_proposal(**kwargs)

    with pytest.raises(FileExistsError, match="Pass overwrite=True"):
        generate_proposal(**kwargs)


def test_script_help_runs_from_repository_root() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/auto_research.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Generate advisory proposals" in completed.stdout
