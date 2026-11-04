from __future__ import annotations

import builtins
from pathlib import Path

import gpc.search_eval as evaluation

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/search_eval_context_engine.yml"


def test_context_engine_fixture_integrity_and_yaml_fallback_parity(monkeypatch):
    original = builtins.__import__
    normal = evaluation.load_search_eval_fixture(FIXTURE)

    def without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("optional PyYAML unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_yaml)
    fallback = evaluation.load_search_eval_fixture(FIXTURE)
    assert normal == fallback

    cases = normal
    assert len(cases) == 12
    assert len({case.query for case in cases}) == 12
    assert all(case.query.strip() and case.expected_paths for case in cases)
    assert all(case.repo == "gpc" for case in cases)
    assert all(case.task in {"debug", "implement", "review", "test"} for case in cases)

    paths = [path for case in cases for path in case.expected_paths]
    assert all(path.startswith("gpc:") for path in paths)
    resolved = {(ROOT / path.removeprefix("gpc:")).resolve() for path in paths}
    assert all(
        path.is_relative_to(ROOT.resolve()) and path.is_file() for path in resolved
    )
    assert len(resolved) >= 8
