"""Retrieval contracts with fake backends, not PostgreSQL engine validation."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from gpc import retrieval, search


def uid(number):
    return str(UUID(int=number))


PROJECT = uid(1000)
OTHER_PROJECT = uid(1001)


def chunk(number, path="src/main.py", *, repo="api", project=PROJECT, **extra):
    return {
        "id": uid(number),
        "project_id": project,
        "file_project_id": project,
        "repo_project_id": project,
        "source_type": "project_file",
        "chunk_repo": repo,
        "file_repo": repo,
        "repo_slug": repo,
        "relative_path": path,
        "content": "generic evidence",
        "title": "main",
        "chunk_type": "function",
        "language": "python",
        **extra,
    }


def point(number, score=0.9, **payload):
    return SimpleNamespace(score=score, payload={"chunk_id": uid(number), **payload})


def like(value, pattern):
    """Model escaped ILIKE literals, not PostgreSQL text tokenization."""
    escaped = False
    regex = ""
    for char in pattern:
        if escaped:
            regex += re.escape(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            regex += ".*" if char == "%" else "." if char == "_" else re.escape(char)
    return re.fullmatch(regex, value, flags=re.IGNORECASE | re.DOTALL) is not None


class FakePostgres:
    """Assert scope SQL contracts and model ownership for adversarial fixtures."""

    def __init__(self):
        self.rows = []
        self.calls = []
        self.fail = None

    @contextmanager
    def connect(self, **kwargs):
        yield self

    def execute(self, sql, params):
        self.calls.append((sql, params))
        assert retrieval.SCOPED_CHUNKS in sql
        assert "f.project_id = c.project_id" in sql
        assert "r.project_id = c.project_id" in sql
        assert "c.source_type = 'project_file'" in sql
        assert "coalesce(c.repo_id, f.repo_id)" in sql
        assert "f.repo_id is null or f.repo_id = r.id" in sql
        assert "r.slug = any(%s::text[])" in sql
        lexical = "ts_rank_cd" in sql
        if self.fail == ("lexical" if lexical else "hydration"):
            raise RuntimeError(f"{self.fail} unavailable")
        if lexical:
            (
                query,
                patterns,
                project,
                repos,
                repos_again,
                query_again,
                patterns_again,
                limit,
            ) = params
            assert query == query_again and patterns == patterns_again
            assert sql.count("escape E'\\\\'") == 6
            assert retrieval.LEXICAL_VECTOR in sql
            assert "order by score desc, c.id asc" in sql
            assert "limit %s" in sql
            assert 1 <= limit <= 300
        else:
            project, repos, repos_again, ids = params
            assert "c.id = any(%s::uuid[])" in sql
        assert repos == repos_again
        rows = [
            row
            for row in self.rows
            if row["project_id"] == project
            and row["file_project_id"] == project
            and row["repo_project_id"] == project
            and row["source_type"] == "project_file"
            and (row["chunk_repo"] or row["file_repo"]) == row["repo_slug"]
            and (row["file_repo"] is None or row["file_repo"] == row["repo_slug"])
            and (repos is None or row["repo_slug"] in repos)
        ]
        if lexical:
            matches = []
            for row in rows:
                corpus = (row["title"] or "") + " " + row["content"]
                # This stand-in is deliberately not a claim about engine ts_rank.
                words = re.findall(r"\w+", query.lower())
                fulltext = bool(words) and all(w in corpus.lower() for w in words)
                literal = any(
                    like(value, pattern)
                    for pattern in patterns
                    for value in (
                        row["relative_path"],
                        row["title"] or "",
                        row["content"],
                    )
                )
                if fulltext or literal:
                    matches.append(
                        {"id": row["id"], "score": float(literal) + 0.1 * fulltext}
                    )
            rows = sorted(matches, key=lambda r: (-r["score"], r["id"]))[:limit]
        else:
            rows = [row for row in rows if row["id"] in ids]
        return SimpleNamespace(fetchall=lambda: rows)


@pytest.fixture
def backend(monkeypatch):
    pg = FakePostgres()
    vector = MagicMock()
    vector.query_points.return_value = SimpleNamespace(points=[])
    embed = MagicMock(return_value=[0.1, 0.2])
    resolve = MagicMock(return_value={"id": PROJECT, "slug": "demo", "name": "Demo"})
    monkeypatch.setattr(search, "resolve_project", resolve)
    monkeypatch.setattr(search, "embed_query", embed)
    monkeypatch.setattr(search, "get_qdrant", lambda: vector)
    monkeypatch.setattr(search, "pg_connection", pg.connect)
    monkeypatch.setattr(retrieval, "pg_connection", pg.connect)
    return SimpleNamespace(pg=pg, vector=vector, embed=embed, resolve=resolve)


def run(**kwargs):
    return search.search_project_context(kwargs.pop("query", "generic"), **kwargs)[1]


def test_semantic_default_preserves_vector_ranking_scores_without_lexical(backend):
    backend.pg.rows = [chunk(1), chunk(2)]
    backend.vector.query_points.return_value.points = [point(2, 0.81), point(1, 0.95)]
    results = run(limit=2)
    assert [(r.chunk_id, r.score) for r in results] == [(uid(2), 0.81), (uid(1), 0.95)]
    assert all(r.retrieval_sources == ("semantic",) for r in results)
    assert len(backend.pg.calls) == 1  # hydration only; lexical is not a dependency
    assert backend.vector.query_points.call_args.kwargs["limit"] == 6
    assert backend.embed.call_args.args == ("generic",)
    assert [(r.chunk_id, r.score) for r in run(limit=2, task="test")] == [
        (uid(2), 0.81),
        (uid(1), 0.95),
    ]
    assert "task:test:ignored-semantic" in run(task="test")[0].ranking_signals


@pytest.mark.parametrize(
    "query",
    [
        "parseHTTPResponse",
        "parse_http_response",
        "src/auth/login.py",
        "where is `src/auth/login.py`?",
        "`parseHTTPResponse()`",
    ],
)
def test_lexical_recovers_literal_identifiers_and_paths_without_vector(backend, query):
    backend.pg.rows = [
        chunk(
            1,
            "src/auth/login.py",
            content="def parseHTTPResponse(): parse_http_response()",
        )
    ]
    assert run(query=query) == []  # vector misses this fixture entirely
    backend.embed.reset_mock()
    backend.vector.reset_mock()
    results = run(query=query, mode="lexical")
    assert [r.chunk_id for r in results] == [uid(1)]
    assert results[0].retrieval_sources == ("lexical",)
    backend.embed.assert_not_called()
    backend.vector.query_points.assert_not_called()


def test_hybrid_rrf_dedup_stable_ties_and_limit(backend):
    backend.pg.rows = [chunk(1), chunk(2), chunk(3)]
    backend.vector.query_points.return_value.points = [point(2), point(2), point(1)]
    # SQL lexical order is 1,2,3; vector order deduplicates to 2,1.
    first = run(mode="hybrid", limit=2)
    second = run(mode="hybrid", limit=2)
    assert first == second
    assert [r.chunk_id for r in first] == [uid(1), uid(2)]
    assert first[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert first[1].score == first[0].score
    assert first[0].retrieval_sources == ("semantic", "lexical")
    assert "rrf:k=60" in first[0].ranking_signals
    assert "semantic:rank=2" in first[0].ranking_signals
    assert len({r.chunk_id for r in first}) == 2


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
@pytest.mark.parametrize(
    "task,favored_path",
    [
        ("test", "tests/test_main.py"),
        ("debug", "src/main.py"),
        ("implement", "src/main.py"),
        ("review", "settings.toml"),
    ],
)
def test_task_preferences_are_modest_transparent_and_keep_other_evidence(
    backend, mode, task, favored_path
):
    backend.pg.rows = [chunk(1, "docs/guide.md"), chunk(2, favored_path)]
    # Empty vector makes hybrid single-channel ranks close enough for 5% preference.
    baseline = run(mode=mode, task="general")
    results = run(mode=mode, task=task)
    assert [r.chunk_id for r in baseline] == [uid(1), uid(2)]
    assert [r.chunk_id for r in results] == [uid(2), uid(1)]
    assert results[0].score == baseline[1].score  # raw score is not fabricated
    assert any(
        f"task:{task}:" in signal and "boost=1.05" in signal
        for signal in results[0].ranking_signals
    )
    assert any("boost=1" in signal for signal in results[1].ranking_signals)
    assert not any("task:" in signal for r in baseline for signal in r.ranking_signals)


@pytest.mark.parametrize("mode", ["semantic", "lexical", "hybrid"])
def test_sql_scope_rejects_foreign_payloads_and_conflicting_ownership(backend, mode):
    backend.pg.rows = [
        chunk(1),
        chunk(2, project=OTHER_PROJECT),
        chunk(3, repo="web"),
        chunk(4, source_type="decision"),
        chunk(5, file_project_id=OTHER_PROJECT),
        chunk(6, repo_project_id=OTHER_PROJECT),
        chunk(7, file_repo="web"),
        chunk(8, chunk_repo=None),  # legacy c.repo_id null, authoritative file repo
    ]
    backend.vector.query_points.return_value.points = [
        point(i, project_id=PROJECT, repo_slug="api") for i in range(1, 10)
    ]  # all payloads claim api, including foreign, conflicting and missing DB rows
    results = run(mode=mode, repo=["API"], limit=100)
    assert [r.chunk_id for r in results] == [uid(1), uid(8)]
    assert all(r.repo_slug == "api" for r in results)
    assert all(
        params[3 if "ts_rank_cd" in sql else 1] == ["api"]
        for sql, params in backend.pg.calls
    )
    if mode != "lexical":
        conditions = backend.vector.query_points.call_args.kwargs["query_filter"].must
        assert conditions[0].match.value == PROJECT
        assert conditions[1].match.value == "project_file"
        assert conditions[2].match.any == ["api"]


def test_semantic_repo_identity_never_uses_payload_fallback(backend):
    backend.pg.rows = [chunk(1, repo="web")]
    backend.vector.query_points.return_value.points = [point(1, repo_slug="spoofed")]
    assert run()[0].repo_slug == "web"


@pytest.mark.parametrize("mode", ["semantic", "lexical", "hybrid"])
def test_same_path_in_different_repos_is_not_deduplicated(backend, mode):
    backend.pg.rows = [chunk(1, "README.md"), chunk(2, "README.md", repo="web")]
    backend.vector.query_points.return_value.points = [
        point(1),
        point(1),
        point(2),
        point(99),
    ]
    results = run(mode=mode, repo=["api", "web"], limit=5)
    assert [r.relative_path for r in results] == ["README.md", "README.md"]
    assert [r.repo_slug for r in results] == ["api", "web"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "bad"},
        {"task": "bad"},
        {"query": ""},
        {"query": " \n\t"},
        {"query": "bad\x00query"},
        {"query": None},
        {"query": "q" * 2049},
        {"query": "x " * 33},
        {"query": "x?" * 33},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"repo": ""},
        {"repo": ["api", " "]},
        {"repo": ["!!!"]},
        {"repo": [None]},
        {"repo": [1]},
        {"repo": ("api",)},
    ],
)
def test_invalid_inputs_fail_before_any_backend(backend, kwargs):
    with pytest.raises(ValueError):
        run(**kwargs)
    backend.resolve.assert_not_called()
    backend.embed.assert_not_called()
    backend.vector.query_points.assert_not_called()
    assert not backend.pg.calls


def test_repo_normalized_once_and_empty_list_is_explicitly_unfiltered(
    backend, monkeypatch
):
    normalize = MagicMock(wraps=search.normalize_slug)
    monkeypatch.setattr(search, "normalize_slug", normalize)
    backend.pg.rows = [chunk(1)]
    backend.vector.query_points.return_value.points = [point(1)]
    assert run(mode="hybrid", repo=["API", "web"])
    assert normalize.call_count == 2
    normalize.reset_mock()
    assert run(mode="hybrid", repo=[])
    normalize.assert_not_called()
    assert backend.pg.calls[-1][1][1:3] == (None, None)


@pytest.mark.parametrize(
    "query", ["100%_safe", "src\\auth\\foo_bar.py", "ação_用户", "!!!", "' OR 1=1;--"]
)
def test_escaping_unicode_punctuation_and_injection_are_literal_and_parameterized(
    backend, query
):
    backend.pg.rows = [chunk(1, content=query), chunk(2, content="unrelated")]
    results = run(mode="lexical", query=query)
    assert uid(1) in [r.chunk_id for r in results]
    assert uid(2) not in [r.chunk_id for r in results]
    sql, params = backend.pg.calls[0]
    assert query not in sql
    assert params[0] == query
    if query == "100%_safe":
        assert params[1] == [r"%100\%\_safe%"]
    if query == "src\\auth\\foo_bar.py":
        assert params[1] == [r"%src\\auth\\foo\_bar.py%"]


def test_malformed_and_missing_vector_payload_ids_are_ignored(backend):
    backend.pg.rows = [chunk(1)]
    backend.vector.query_points.return_value.points = [
        SimpleNamespace(score=1, payload=None),
        SimpleNamespace(score=1, payload={"chunk_id": "not-a-uuid"}),
        point(1),
    ]
    assert [r.chunk_id for r in run()] == [uid(1)]


@pytest.mark.parametrize(
    "mode,failed",
    [
        ("semantic", "vector"),
        ("hybrid", "vector"),
        ("lexical", "lexical"),
        ("hybrid", "lexical"),
        ("hybrid", "hydration"),
    ],
)
def test_backend_errors_are_not_hidden_as_full_or_partial_success(
    backend, mode, failed
):
    backend.pg.rows = [chunk(1)]
    backend.vector.query_points.return_value.points = [point(1)]
    if failed == "vector":
        backend.vector.query_points.side_effect = RuntimeError("vector unavailable")
    else:
        backend.pg.fail = failed
    with pytest.raises(RuntimeError, match=f"{failed} unavailable"):
        run(mode=mode)


def test_embedding_failure_propagates_but_lexical_is_usable(backend):
    backend.pg.rows = [chunk(1)]
    backend.embed.side_effect = RuntimeError("embedding unavailable")
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        run(mode="hybrid")
    assert run(mode="lexical")


def test_candidate_bounds_and_result_limit(backend):
    backend.pg.rows = [chunk(i) for i in range(1, 320)]
    # Real Qdrant enforces the requested limit; fake honors that here too.
    backend.vector.query_points.return_value.points = [point(i) for i in range(1, 301)]
    assert len(run(mode="hybrid", limit=100)) == 100
    assert backend.vector.query_points.call_args.kwargs["limit"] == 300
    assert backend.pg.calls[0][1][-1] == 300
    assert len(backend.pg.calls[1][1][-1]) <= 600


@pytest.mark.parametrize("mode", search.SEARCH_MODES)
@pytest.mark.parametrize("task", search.SEARCH_TASKS)
def test_candidate_capture_preserves_results_budget_and_backend_calls(
    backend, mode, task
):
    backend.pg.rows = [
        chunk(1, "src/main.py"),
        chunk(2, "tests/test_main.py"),
        chunk(3, "docs/main.md"),
    ]
    backend.vector.query_points.return_value.points = [point(3), point(1), point(2)]
    baseline = search.search_project_context("generic", limit=2, mode=mode, task=task)
    calls = list(backend.pg.calls)
    backend.pg.calls.clear()
    backend.vector.query_points.reset_mock()
    backend.embed.reset_mock()
    trace = {"stale": True}
    captured = search.search_project_context(
        "generic", limit=2, mode=mode, task=task, candidate_trace=trace
    )
    assert captured == baseline  # full frozen SearchResult equality, not just IDs
    assert backend.pg.calls == calls
    assert len(calls) == (1 if mode == "semantic" else 2)
    assert backend.vector.query_points.call_count == (mode != "lexical")
    assert backend.embed.call_count == (mode != "lexical")
    assert trace["candidate_limit"] == 6 and trace["k"] == 2
    assert len(trace["ranked_candidates"]) == 3
    assert [r["chunk_id"] for r in trace["ranked_candidates"][:2]] == [
        r.chunk_id for r in baseline[1]
    ]
    assert "stale" not in trace


def test_candidate_capture_dedup_and_hydration_privacy(backend):
    backend.pg.rows = [
        chunk(1, "ok.py", content="content-canary", title="title-canary"),
        chunk(2, "foreign-path-canary", project=OTHER_PROJECT),
        chunk(3, "wrong-repo-canary", repo="web"),
    ]
    backend.vector.query_points.return_value.points = [
        point(1, 0.9, source="source-canary"),
        point(1, 0.1),
        point(2),
        point(3),
        point(99),
    ]
    trace = {}
    results = run(repo="api", limit=2, candidate_trace=trace)
    assert len(results) == 1
    channel = trace["channels"]["semantic"]
    assert {k: v for k, v in channel.items() if k != "candidates"} == {
        "raw_returned": 5,
        "unique_returned": 4,
        "duplicates_returned": 1,
        "hydrated": 1,
        "dropped": 3,
    }
    assert channel["candidates"][0]["score"] == 0.9
    serialized = json.dumps(trace, allow_nan=False)
    for secret in (
        "content-canary",
        "title-canary",
        "source-canary",
        "foreign-path-canary",
        "wrong-repo-canary",
        uid(2),
        uid(3),
        uid(99),
    ):
        assert secret not in serialized
    # A later mutation cannot change the capture's copied safe metadata.
    backend.pg.rows[0]["relative_path"] = "mutated"
    assert channel["candidates"][0]["relative_path"] == "ok.py"


@pytest.mark.parametrize("mode", search.SEARCH_MODES)
@pytest.mark.parametrize("stale", [False, True])
def test_candidate_capture_empty_and_all_dropped(backend, monkeypatch, mode, stale):
    if stale:
        backend.vector.query_points.return_value.points = [point(99)]
        monkeypatch.setattr(
            search, "lexical_candidates", lambda *a, **kw: [(uid(99), 1)]
        )
    trace = {"stale": True}
    assert run(mode=mode, candidate_trace=trace) == []
    assert trace["ranked_candidates"] == []
    for channel in trace["channels"].values():
        assert channel["candidates"] == []
        assert channel["dropped"] == int(stale)
    assert uid(99) not in json.dumps(trace)


def test_candidate_trace_cleared_on_backend_error_and_invalid_input(backend):
    trace = {"previous_success": True}
    backend.vector.query_points.side_effect = RuntimeError("backend unavailable")
    with pytest.raises(RuntimeError):
        run(candidate_trace=trace)
    assert trace == {}
    backend.resolve.reset_mock()
    with pytest.raises(ValueError):
        run(limit=0, candidate_trace=trace)
    backend.resolve.assert_not_called()
    with pytest.raises(ValueError):
        run(candidate_trace=[])
    backend.resolve.assert_not_called()


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_capture_fails_without_publishing_partial_success(backend, score):
    backend.pg.rows = [chunk(1)]
    backend.vector.query_points.return_value.points = [point(1, score)]
    trace = {"old": True}
    with pytest.raises(ValueError):
        run(candidate_trace=trace)
    assert trace == {}


def test_compose_forwards_nondefaults_and_graph_enrichment_stays_after_ranking(
    backend, monkeypatch
):
    backend.pg.rows = [chunk(1, "docs/main.md"), chunk(2, "tests/test_main.py")]
    annotations = MagicMock(side_effect=lambda **kw: {1: "graph evidence"})
    monkeypatch.setattr(search, "_graph_annotations", annotations)
    _, results, block = search.compose_project_context(
        "generic", mode="hybrid", task="test", include_graph=True, max_chars=1000
    )
    assert results[0].chunk_id == uid(2)
    assert results[0].graph_notes == "graph evidence"
    assert results[0].retrieval_sources == ("lexical",)
    assert annotations.call_args.kwargs["results"][0].chunk_id == uid(2)
    assert len(block) <= 1000
    assert "tests/test_main.py" in block
    # Preserve existing strict-signature mocks in default mode.
    default_mock = MagicMock(return_value=({"slug": "p", "name": "P"}, []))
    monkeypatch.setattr(search, "search_project_context", default_mock)
    search.compose_project_context("q")
    assert "mode" not in default_mock.call_args.kwargs
    assert "task" not in default_mock.call_args.kwargs


def test_api_constants_defaults_and_migration_index_expression():
    assert search.SEARCH_MODES == ("semantic", "lexical", "hybrid")
    assert search.SEARCH_TASKS == ("general", "debug", "implement", "review", "test")
    old_positional = search.SearchResult(
        0.9, "id", "a.py", None, "x", "file", None, "api", "graph"
    )
    assert old_positional.graph_notes == "graph"
    assert old_positional.retrieval_sources == old_positional.ranking_signals == ()
    migration = (
        Path(__file__).parents[1] / "migrations/0010_lexical_search.sql"
    ).read_text()
    assert retrieval.LEXICAL_VECTOR.replace("c.", "") in migration
    assert "using gin" in migration
    assert "where source_type = 'project_file'" in migration
