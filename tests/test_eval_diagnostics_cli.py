from __future__ import annotations

from argparse import Namespace
import json

import pytest

from gpc.cli import build_parser, cmd_eval_search
from gpc.eval_corpus import EvalCorpusSnapshot
import gpc.search_eval as evaluation


def args(*flags):
    return build_parser().parse_args(
        ["eval-search", "--project", "p", "--fixture", "unused", *flags]
    )


@pytest.mark.parametrize(
    "flags",
    [
        ("--compare-all", "--mode", "semantic"),
        ("--compare-all", "--compare"),
        ("--compare", "--mode", "lexical"),
    ],
)
def test_compare_flags_mutually_exclusive(flags):
    with pytest.raises(SystemExit):
        args(*flags)


def sample_report(mode="lexical", diagnose=True):
    case = evaluation.SearchEvalCase("q", ["available.py", "absent.py"])
    report = evaluation.compute_search_eval(
        [case],
        results_by_query={},
        k=5,
        mode=mode,
        provider="test",
        model="test",
        dimensions=0,
    )
    if not diagnose:
        return report
    from dataclasses import replace

    snapshot = EvalCorpusSnapshot(
        "p", "p", (("r", "repo"),), (("repo", "available.py", "hash", True),)
    )
    diagnostics = snapshot.diagnose_case(case, case.expected_paths, 0)
    return replace(
        report,
        results=[replace(report.results[0], diagnostics=diagnostics)],
        diagnostics={
            "snapshot": snapshot.to_dict("fixture"),
            "coverage": diagnostics["coverage"],
        },
    )


def test_single_diagnostics_json_and_readable_coverage(monkeypatch, capsys):
    calls = []

    def run(*a, **kw):
        calls.append(kw)
        return sample_report()

    monkeypatch.setattr(evaluation, "run_search_eval", run)
    assert cmd_eval_search(args("--mode", "lexical", "--diagnose", "--json")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"]["coverage"]["not_indexed"] == 1
    assert calls[-1]["diagnose"] is True
    cmd_eval_search(args("--mode", "lexical", "--diagnose"))
    text = capsys.readouterr().out
    assert "diagnostic_coverage indexed=1" in text
    assert "case=0" in text and "absent.py=not_indexed" in text
    assert "available.py=indexed_but_not_retrieved" in text
    assert "not proof of Qdrant" in text


def test_compare_all_forwards_optins_and_prints_all_modes(monkeypatch, capsys):
    calls = []
    payload = {
        "baseline": sample_report("semantic").to_dict(),
        "candidate": sample_report("hybrid").to_dict(),
        "lexical": sample_report().to_dict(),
        "delta": {"recall_at_k": 0.0},
        "lexical_delta": {"recall_at_k": 0.0},
        "comparison_note": "Fixed order: semantic, hybrid, lexical; not a benchmark.",
    }

    def compare(*a, **kw):
        calls.append(kw)
        return payload

    monkeypatch.setattr(evaluation, "compare_search_eval", compare)
    assert cmd_eval_search(args("--compare-all", "--diagnose")) == 0
    text = capsys.readouterr().out
    assert calls[-1] == {
        "k": 5,
        "task": "general",
        "diagnose": True,
        "include_lexical": True,
    }
    assert "baseline mode=semantic" in text and "candidate mode=hybrid" in text
    assert "lexical mode=lexical" in text and "lexical_delta" in text
    assert text.count("diagnostic_coverage") == 3
    cmd_eval_search(args("--compare-all", "--json"))
    assert json.loads(capsys.readouterr().out) == payload


def test_legacy_direct_namespace_no_new_kwargs_or_payload_keys(monkeypatch, capsys):
    calls = []

    def run(*a, **kw):
        calls.append(kw)
        return sample_report(diagnose=False)

    monkeypatch.setattr(evaluation, "run_search_eval", run)
    namespace = Namespace(
        project="p",
        fixture="unused",
        k=5,
        mode="lexical",
        task="general",
        compare=False,
        json=True,
    )
    cmd_eval_search(namespace)
    assert calls == [{"k": 5, "mode": "lexical", "task": "general"}]
    payload = json.loads(capsys.readouterr().out)
    assert "diagnostics" not in payload and "diagnostics" not in payload["results"][0]


@pytest.mark.parametrize(
    "flags", [("--mode", "lexical"), ("--compare",), ("--compare-all",)]
)
def test_candidate_analysis_implies_diagnose_for_all_cli_modes(
    monkeypatch, capsys, flags
):
    from dataclasses import replace

    report = replace(
        sample_report(),
        candidate_analysis={
            "candidate_pool": {"hits": 1, "expected_paths": 2, "total_available": 3},
            "caps": {"cap_1": {"recall_at_k": 0.5}, "cap_2": {"recall_at_k": 0.5}},
            "note": "Offline shortlist caps are not default retrieval; no refill.",
        },
    )
    calls = []

    def run(*a, **kw):
        calls.append(kw)
        return report

    def compare(*a, **kw):
        calls.append(kw)
        return {
            "baseline": report.to_dict(),
            "candidate": report.to_dict(),
            "lexical": report.to_dict(),
            "delta": {"recall_at_k": 0.0},
            "lexical_delta": {"recall_at_k": 0.0},
            "comparison_note": "offline",
        }

    monkeypatch.setattr(evaluation, "run_search_eval", run)
    monkeypatch.setattr(evaluation, "compare_search_eval", compare)
    assert cmd_eval_search(args(*flags, "--analyze-candidates")) == 0
    output = capsys.readouterr().out
    assert calls[-1]["diagnose"] is calls[-1]["analyze_candidates"] is True
    assert "candidate_coverage hits=1/2 total_available=3" in output
    assert "offline_cap_1" in output and "offline_cap_2" in output
    assert "not default retrieval" in output
    assert cmd_eval_search(args(*flags, "--analyze-candidates", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    if "--mode" not in flags:
        payload = payload["baseline"]
    assert payload["candidate_analysis"] == report.candidate_analysis


def test_candidate_analysis_help_explains_implied_diagnostics(capsys):
    with pytest.raises(SystemExit):
        args("--help")
    output = capsys.readouterr().out
    assert "--analyze-candidates" in output
    assert "implies --diagnose" in output
