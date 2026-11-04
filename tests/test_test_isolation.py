from __future__ import annotations

import ast
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_TESTS = {
    "tests/test_graphify_freshness.py": {
        "test_measure_sync_freshness_reports_queryable_lag",
    },
    "tests/test_graph_test_portfolio.py": {
        "test_generate_test_portfolio_report_flags_uncovered_external_call",
        "test_generate_test_portfolio_report_treats_referenced_module_as_covered",
    },
    "tests/test_multi_repo_graph_path.py": {
        "test_graph_path_crosses_single_bridge_between_repos",
    },
}


def test_default_selection_excludes_live_services(pytestconfig) -> None:
    # Inspect repository configuration, not command-line overrides or a nested run.
    addopts = shlex.split(pytestconfig.inicfg["addopts"])
    assert "--ignore=tests/smoke" in addopts
    assert addopts[addopts.index("-m") + 1] == "not integration"
    assert any(
        marker.startswith("integration:") for marker in pytestconfig.inicfg["markers"]
    )


def test_only_live_graph_tests_are_marked_integration() -> None:
    for relative_path, expected in INTEGRATION_TESTS.items():
        module = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        marked = {
            node.name
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and any(
                ast.unparse(decorator) == "pytest.mark.integration"
                for decorator in node.decorator_list
            )
        }
        assert marked == expected, relative_path


def test_ci_runs_all_graph_integration_tests_on_disposable_services() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    integration_job = workflow.split("\n  integration:\n", 1)[1]
    assert "python -m pip install -r requirements.txt -r requirements-dev.txt" in (
        integration_job
    )
    step = integration_job.split("- name: Run graph integration tests\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    command = " ".join(
        line.strip() for line in step.split("run: >-\n", 1)[1].splitlines()
    )
    assert shlex.split(command) == [
        "python",
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-m",
        "integration",
        *INTEGRATION_TESTS,
    ]
