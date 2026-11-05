"""Frozen weighted policy contracts; entirely offline, not a PostgreSQL plan test."""

from contextlib import contextmanager
from dataclasses import replace
import json
import re
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from gpc import retrieval, search
from gpc.cli import build_parser, cmd_eval_search
from gpc.eval_candidates import build_capture, capture_sha256
from gpc.eval_corpus import EvalCorpusSnapshot
import gpc.search_eval as evaluation


@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "the src/ação_50%.py parseHTTP snake_case",
            ["src/ação_50%.py", "parseHTTP", "snake_case"],
        ),
        ("CODE code Code código CÓDIGO", ["CODE", "código"]),
        (
            "where file code watch decision",
            ["where", "file", "code", "watch", "decision"],
        ),
        ("o arquivo com código", ["arquivo", "código"]),
        ("in", ["in"]),
        ("is", ["is"]),
        ("the in a", []),
        ("É são uma", []),
    ],
)
def test_frozen_term_policy(query, expected):
    assert retrieval.weighted_query_terms(retrieval.query_terms(query)) == expected


def test_policy_identity_and_exact_function_word_set():
    assert retrieval.WEIGHTED_POLICY_VERSION == "weighted-v1"
    assert retrieval.WEIGHTED_STOPWORDS == frozenset(
        "a an the i me my we us our you your he him his she her it its they them their "
        "of in on at to for from by with as is are was were be been being am do does "
        "did have has had can could will would shall should may might must "
        "o os a as um uma uns umas eu me mim meu minha nós nos nosso nossa você vocês "
        "ele ela eles elas seu sua seus suas de do da dos das em no na nos nas ao aos "
        "à às para por pelo pela pelos pelas com sem entre sobre sob até é são era "
        "eram ser sido sendo estou está estamos estão estar estava estavam tenho tem "
        "temos têm ter tinha tinham foi foram fui fomos pode podem poderia poderiam".split()
    )


class RecordingDB:
    def __init__(self):
        self.calls = []

    @contextmanager
    def connect(self, **kw):
        yield self

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return SimpleNamespace(fetchall=lambda: [])


def weighted(db, monkeypatch, query, **kw):
    monkeypatch.setattr(retrieval, "pg_connection", db.connect)
    return retrieval.lexical_candidates(
        query,
        retrieval.query_terms(query),
        project_id=kw.pop("project_id", "project"),
        repo_slugs=kw.pop("repo_slugs", ["repo"]),
        limit=kw.pop("limit", 15),
        lexical_strategy="weighted",
        **kw,
    )


def test_sql_binding_escape_scope_cap_and_no_fulltext(monkeypatch):
    db = RecordingDB()
    query = r"src/ação_50%.py snake_case x\y ');DROP --"
    project = "project'); DROP TABLE gpc_chunks;--"
    repos = ["repo'); DROP TABLE gpc_repos;--"]
    assert (
        weighted(
            db, monkeypatch, query, project_id=project, repo_slugs=repos, limit=999
        )
        == []
    )
    sql, params = db.calls[0]
    assert params == (
        project,
        repos,
        repos,
        ["%src/ação\\_50\\%.py%", "%snake\\_case%", "%x\\\\y%", "%DROP%", "%--%"],
        300,
    )
    assert project not in sql and repos[0] not in sql and "ação" not in sql
    assert retrieval.SCOPED_CHUNKS in sql
    assert sql.count(r"escape E'\\'") == 3
    assert "ts_rank" not in sql and "to_tsvector" not in sql
    assert "select id::text as id, score from scored" in sql
    assert "where score > 0" in sql
    assert (
        "order by score desc, repo_slug, relative_path, chunk_index, content_hash, id"
        in sql
    )


def test_all_stopwords_skip_database(monkeypatch):
    db = RecordingDB()
    assert weighted(db, monkeypatch, "the in a") == []
    assert db.calls == []


def oracle(terms, path, title, content):
    """Independent policy definition: substring membership, no SQL inspection."""
    return sum(
        next(
            (
                weight
                for field, weight in ((path, 6), (title or "", 3), (content, 1))
                if term.casefold() in field.casefold()
            ),
            0,
        )
        for term in terms
    )


def test_sql_case_scoring_against_independent_oracle(monkeypatch):
    """Execute emitted CASE/SUM on SQLite after explicit dialect substitutions.

    This tests score arithmetic/precedence, NOT PostgreSQL ILIKE, its planner,
    ownership joins or service integration. The unmodified SQL needs a separate
    disposable-PostgreSQL check before any evaluation run.
    """
    db = RecordingDB()
    query = "alpha ALPHA beta beta snake_case 50% ação"
    weighted(db, monkeypatch, query)
    sql, params = db.calls[0]
    expression = sql.split("select sum(case", 1)[1].split(
        "from unnest(%s::text[]) as terms(pattern)", 1
    )[0]
    expression = "select sum(case" + expression + " from patterns as terms"
    for field in ("coalesce(relative_path, '')", "coalesce(title, '')", "content"):
        expression = expression.replace(
            field + r" ilike terms.pattern escape E'\\'",
            f"literal_ilike({field}, terms.pattern)",
        )
    conn = sqlite3.connect(":memory:")

    def literal_ilike(value, pattern):
        # Decode only the escaped literal pattern produced by the real helper.
        literal = re.sub(r"\\(.)", r"\1", pattern[1:-1])
        return int(literal.casefold() in (value or "").casefold())

    conn.create_function("literal_ilike", 2, literal_ilike)
    conn.execute("create table patterns (pattern text)")
    conn.executemany("insert into patterns values (?)", [(p,) for p in params[3]])
    terms = ["alpha", "beta", "snake_case", "50%", "ação"]
    rows = [
        ("alpha/beta.py", "alpha", "alpha " * 100),
        ("unrelated.py", "alpha beta", "alpha beta " * 100),
        ("other.py", None, "alpha beta " * 100),
        ("snake_case/50%.py", "ação", "snake_case 50% ação"),
        ("snakeXcase/50Z.py", "", "nothing"),
        ("AÇÃO.py", "beta", "alpha"),
    ]
    for path, title, content in rows:
        actual = conn.execute(
            "select ("
            + expression
            + ") from (select ? as relative_path, ? as title, ? as content)",
            (path, title, content),
        ).fetchone()[0]
        assert actual == oracle(terms, path, title, content)
    conn.close()


def row(identifier, repo="one", path="same.py", index=0, digest="hash"):
    return {
        "id": identifier,
        "repo_slug": repo,
        "relative_path": path,
        "chunk_index": index,
        "content_hash": digest,
        "title": "title-canary",
        "content": "content-canary",
        "chunk_type": "function",
        "language": "python",
    }


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
@pytest.mark.parametrize("task", search.SEARCH_TASKS)
def test_weighted_stable_hydrated_ties_and_safe_capture(monkeypatch, mode, task):
    monkeypatch.setattr(search, "resolve_project", lambda **kw: {"id": "p"})
    semantic = Mock(return_value=[])
    monkeypatch.setattr(search, "_semantic_candidates", semantic)
    lexical = Mock()
    monkeypatch.setattr(search, "lexical_candidates", lexical)
    fetch = Mock()
    monkeypatch.setattr(search, "_fetch_chunks", fetch)
    identities = []
    for numbers in ((26, 1, 2, 3), (1, 26, 25, 24)):
        ids = tuple(str(UUID(int=number)) for number in numbers)
        rows = [
            row(ids[0]),
            row(ids[1], index=1),
            row(ids[2], repo="two"),
            row(ids[3], digest="aaa"),
        ]
        # Equal lexical scores, or exact cross-channel RRF ties in hybrid.
        lexical.return_value = [(ids[2], 1), (ids[0], 1), (ids[1], 1), (ids[3], 1)]
        if mode == "hybrid":
            semantic.return_value = list(reversed(lexical.return_value))
        fetch.return_value = list(reversed(rows))
        trace = {"stale": "must disappear"}
        _, found = search.search_project_context(
            "code",
            mode=mode,
            task=task,
            limit=4,
            lexical_strategy="weighted",
            candidate_trace=trace,
        )
        identities.append(
            [
                (
                    r.repo_slug,
                    r.relative_path,
                    next(c["chunk_index"] for c in rows if c["id"] == r.chunk_id),
                )
                for r in found
            ]
        )
        assert trace["version"] == 2
        assert trace["lexical_strategy"] == "weighted"
        assert trace["lexical_policy_version"] == "weighted-v1"
        assert "experimental" in trace["lexical_tie_policy"]
        assert all(
            secret not in json.dumps(trace)
            for secret in ("title-canary", "content-canary", "stale")
        )
        assert all("lexical-policy:weighted-v1" in r.ranking_signals for r in found)
        assert lexical.call_args.kwargs["limit"] == 12
        assert lexical.call_args.kwargs["lexical_strategy"] == "weighted"
        altered = {**trace, "lexical_policy_version": "different"}
        assert capture_sha256(altered) != capture_sha256(trace)
    assert identities[0] == identities[1]
    if mode == "lexical":
        assert [r.chunk_id for r in found] == [
            str(UUID(int=number)) for number in (24, 1, 26, 25)
        ]
        semantic.assert_not_called()
    else:
        for result in found:
            ranks = [
                next(
                    i
                    for i, (identifier, _) in enumerate(candidates, 1)
                    if identifier == result.chunk_id
                )
                for candidates in (semantic.return_value, lexical.return_value)
            ]
            assert result.score == pytest.approx(sum(1 / (60 + rank) for rank in ranks))


def test_empty_weighted_trace_and_default_capture_v1(monkeypatch):
    monkeypatch.setattr(search, "resolve_project", lambda **kw: {"id": "p"})
    db = RecordingDB()
    monkeypatch.setattr(retrieval, "pg_connection", db.connect)
    fetch = Mock(side_effect=AssertionError("no hydration"))
    monkeypatch.setattr(search, "_fetch_chunks", fetch)
    trace = {}
    assert (
        search.search_project_context(
            "the in a",
            mode="lexical",
            lexical_strategy="weighted",
            candidate_trace=trace,
        )[1]
        == []
    )
    assert db.calls == []
    assert trace["channels"]["lexical"]["raw_returned"] == 0
    assert trace["version"] == 2
    default = build_capture(
        {}, [], mode="semantic", task="general", k=5, candidate_limit=15
    )
    assert set(default) == {
        "version",
        "mode",
        "task",
        "k",
        "candidate_limit",
        "channels",
        "ranked_candidates",
    }
    assert default["version"] == 1


@pytest.mark.parametrize(
    "mode,strategy", [("semantic", "weighted"), ("semantic", "bad"), ("lexical", "bad")]
)
def test_search_strategy_validation_precedes_backend(monkeypatch, mode, strategy):
    resolve = Mock(side_effect=AssertionError("no backend"))
    monkeypatch.setattr(search, "resolve_project", resolve)
    with pytest.raises(ValueError, match="lexical_strategy"):
        search.search_project_context("query", mode=mode, lexical_strategy=strategy)
    resolve.assert_not_called()


def test_eval_validation_precedes_fixture_and_services(monkeypatch):
    load = Mock(side_effect=AssertionError("no fixture/backend"))
    monkeypatch.setattr(evaluation, "load_search_eval_fixture", load)
    monkeypatch.setattr(evaluation, "load_eval_corpus", load)
    with pytest.raises(ValueError, match="not applicable"):
        evaluation.run_search_eval(
            "p", "missing", lexical_strategy="weighted", diagnose=True
        )
    with pytest.raises(ValueError, match="lexical_strategy"):
        evaluation.compare_search_eval(
            "p", "missing", lexical_strategy="bad", diagnose=True
        )
    load.assert_not_called()


@pytest.mark.parametrize("without_pyyaml", [False, True])
def test_eval_strategy_forwarding_legacy_baseline_and_report(
    tmp_path, monkeypatch, without_pyyaml
):
    if without_pyyaml:
        monkeypatch.setitem(sys.modules, "yaml", None)
    fixture = tmp_path / "queries.yaml"
    fixture.write_text(
        'queries:\n  - query: "code"\n    expected_paths: ["one:same.py", "two:same.py"]\n'
    )
    calls = []
    monkeypatch.setattr(evaluation, "active_embedding_model", lambda: "offline")
    monkeypatch.setattr(evaluation, "embedding_dimension", lambda: 2)

    def retrieve(query, **kw):
        calls.append(kw)
        return {}, [
            search.SearchResult(
                1, "id", "same.py", None, "", "function", "python", "one"
            )
        ]

    monkeypatch.setattr(evaluation, "search_project_context", retrieve)
    report = evaluation.compare_search_eval(
        "p", fixture, include_lexical=True, lexical_strategy="weighted"
    )
    assert [kw["mode"] for kw in calls] == ["semantic", "hybrid", "lexical"]
    assert "lexical_strategy" not in calls[0]
    assert all(kw["lexical_strategy"] == "weighted" for kw in calls[1:])
    assert "lexical_strategy" not in report["baseline"]
    for name in ("candidate", "lexical"):
        assert report[name]["lexical_policy_version"] == "weighted-v1"
        assert report[name]["summary"]["recall_at_k"] == 0.5
    assert len(calls) == 3
    calls.clear()
    default = evaluation.run_search_eval("p", fixture)
    assert (
        "lexical_strategy" not in calls[0]
        and "lexical_strategy" not in default.to_dict()
    )


def cli_args(*flags):
    return build_parser().parse_args(
        ["eval-search", "--project", "p", "--fixture", "unused", *flags]
    )


def test_cli_strategy_validation_and_provenance(monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(evaluation, "run_search_eval", run)
    with pytest.raises(ValueError, match="not applicable"):
        cmd_eval_search(cli_args("--lexical-strategy", "weighted", "--diagnose"))
    run.assert_not_called()
    report = evaluation.compute_search_eval(
        [], results_by_query={}, k=5, mode="lexical"
    )
    run.return_value = replace(report, lexical_strategy="weighted")
    cmd_eval_search(cli_args("--mode", "lexical", "--lexical-strategy", "weighted"))
    text = capsys.readouterr().out
    assert (
        "lexical_strategy=weighted" in text
        and "lexical_policy_version=weighted-v1" in text
    )
    assert run.call_args.kwargs["lexical_strategy"] == "weighted"
    cmd_eval_search(
        cli_args("--mode", "lexical", "--lexical-strategy", "weighted", "--json")
    )
    assert (
        json.loads(capsys.readouterr().out)["lexical_policy_version"] == "weighted-v1"
    )


def test_cli_compare_weighted_preserves_semantic_baseline(monkeypatch, capsys):
    report = evaluation.compute_search_eval(
        [], results_by_query={}, k=5, mode="lexical"
    )
    payload = {
        "baseline": replace(report, mode="semantic").to_dict(),
        "candidate": replace(
            report, mode="hybrid", lexical_strategy="weighted"
        ).to_dict(),
        "lexical": replace(report, lexical_strategy="weighted").to_dict(),
        "delta": {},
        "lexical_delta": {},
        "comparison_note": "offline",
    }
    compare = Mock(return_value=payload)
    monkeypatch.setattr(evaluation, "compare_search_eval", compare)
    cmd_eval_search(cli_args("--compare-all", "--lexical-strategy", "weighted"))
    text = capsys.readouterr().out
    assert text.count("lexical_policy_version=weighted-v1") == 2
    assert compare.call_args.kwargs == {
        "k": 5,
        "task": "general",
        "include_lexical": True,
        "lexical_strategy": "weighted",
    }


@pytest.mark.parametrize("query", ["x" * 2049, " ".join(["x"] * 33), "x\x00y", " "])
def test_weighted_query_bounds_before_backend(monkeypatch, query):
    backend = Mock(side_effect=AssertionError("backend must not run"))
    monkeypatch.setattr(search, "resolve_project", backend)
    with pytest.raises(ValueError):
        search.search_project_context(query, mode="hybrid", lexical_strategy="weighted")
    backend.assert_not_called()


def test_weighted_analysis_hashes_capture_without_extra_queries(monkeypatch):
    cases = [
        evaluation.SearchEvalCase("code", ["one:same.py", "two:same.py", "absent.py"])
    ]
    monkeypatch.setattr(
        evaluation, "_load_fixture_snapshot", lambda path: (cases, "fingerprint")
    )
    snapshot = EvalCorpusSnapshot(
        "p",
        "p",
        (("r1", "one"), ("r2", "two")),
        (("one", "same.py", "hash", True), ("two", "same.py", "other", True)),
    )
    load = Mock(return_value=snapshot)
    monkeypatch.setattr(evaluation, "load_eval_corpus", load)
    monkeypatch.setattr(evaluation, "active_embedding_model", lambda: "offline")
    monkeypatch.setattr(evaluation, "embedding_dimension", lambda: 2)
    monkeypatch.setattr(search, "resolve_project", lambda **kw: {"id": "p"})
    lexical = Mock(return_value=[("a", 6), ("b", 3)])
    semantic = Mock(return_value=[("b", 0.9), ("a", 0.8)])
    hydrate = Mock(return_value=[row("a"), row("b", repo="two")])
    monkeypatch.setattr(search, "lexical_candidates", lexical)
    monkeypatch.setattr(search, "_semantic_candidates", semantic)
    monkeypatch.setattr(search, "_fetch_chunks", hydrate)
    monkeypatch.setattr(
        evaluation, "search_project_context", search.search_project_context
    )
    report = evaluation.compare_search_eval(
        "p",
        "unused",
        k=2,
        include_lexical=True,
        lexical_strategy="weighted",
        analyze_candidates=True,
    )
    assert lexical.call_count == semantic.call_count == 2
    assert hydrate.call_count == 3
    load.assert_called_once()
    assert all(
        call.kwargs["limit"] == 6
        for call in lexical.call_args_list + semantic.call_args_list
    )
    for name, version in (("baseline", 1), ("candidate", 2), ("lexical", 2)):
        result = report[name]["results"][0]
        analysis = result["candidate_analysis"]
        capture = analysis["capture"]
        assert capture["version"] == version
        assert analysis["capture_sha256"] == capture_sha256(capture)
        assert analysis["candidate_pool"]["hit_paths"] == ["one:same.py", "two:same.py"]
        assert report[name]["summary"]["recall_at_k"] == 2 / 3
        assert "title-canary" not in json.dumps(report)
        assert "content-canary" not in json.dumps(report)
        if version == 2:
            assert (
                capture["lexical_policy_version"]
                == report[name]["lexical_policy_version"]
            )
        else:
            assert "lexical_strategy" not in capture
