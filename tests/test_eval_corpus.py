from __future__ import annotations

from dataclasses import replace
import json
import re
import sqlite3

import pytest

import gpc.eval_corpus as corpus
from gpc.registry import ProjectResolutionError
from gpc.search_eval import SearchEvalCase


class SQLiteMetadata:
    """Exercise metadata SQL ownership/predicates without a backing service."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.calls = []
        self.db.executescript("""
            create table gpc_projects(id text, slug text);
            create table gpc_project_aliases(project_id text, alias text);
            create table gpc_repos(id text, project_id text, slug text);
            create table gpc_files(id text, project_id text, repo_id text,
                                   relative_path text, content_hash text);
            create table gpc_chunks(id text, project_id text, repo_id text,
                                    file_id text, source_type text);
            insert into gpc_projects values ('p', 'project'), ('other', 'other');
            insert into gpc_project_aliases values ('p', 'alias'), ('other', 'project');
            insert into gpc_repos values ('a', 'p', 'repo-a'), ('b', 'p', 'repo-b'),
                                         ('foreign', 'other', 'repo-a');
            insert into gpc_files values
                ('indexed', 'p', 'a', 'indexed.py', 'hash-1'),
                ('empty', 'p', 'a', 'empty.py', 'hash-2'),
                ('shared-a', 'p', 'a', 'shared.py', 'hash-3'),
                ('shared-b', 'p', 'b', 'shared.py', 'hash-4'),
                ('foreign-file', 'other', 'foreign', 'foreign.py', 'secret-hash'),
                ('bad-repo', 'p', 'foreign', 'bad.py', 'bad-hash'),
                ('legacy', 'p', 'a', 'legacy.py', 'hash-5'),
                ('legacy-file', 'p', null, 'legacy-file.py', 'hash-6');
            insert into gpc_chunks values
                ('c1', 'p', 'a', 'indexed', 'project_file'),
                ('c2', 'p', 'a', 'indexed', 'project_file'),
                ('c3', 'p', 'a', 'shared-a', 'project_file'),
                ('c4', 'p', 'b', 'shared-b', 'project_file'),
                ('wrong-project', 'other', 'a', 'empty', 'project_file'),
                ('wrong-repo', 'p', 'b', 'empty', 'project_file'),
                ('wrong-type', 'p', 'a', 'empty', 'conversation'),
                ('legacy', 'p', null, 'legacy', 'project_file'),
                ('legacy-file', 'p', 'a', 'legacy-file', 'project_file');
            """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.lower().startswith("set "):
            return self
        sql = re.sub(r"::(?:text|uuid)(?:\[\])?", "", sql)
        if "any(%s)" in sql:
            values = params[-1]
            sql = sql.replace("= any(%s)", f"in ({','.join('?' for _ in values)})")
            params = (*params[:-1], *values)
        cursor = self.db.execute(sql.replace("%s", "?"), params)

        class Rows:
            def fetchone(self):
                row = cursor.fetchone()
                return dict(row) if row is not None else None

            def fetchall(self):
                rows = [dict(row) for row in cursor.fetchall()]
                for row in rows:
                    if "has_chunks" in row:
                        row["has_chunks"] = bool(row["has_chunks"])
                return rows

        return Rows()


@pytest.fixture
def metadata(monkeypatch):
    connection = SQLiteMetadata()
    calls = []

    def connect(*args, **kwargs):
        calls.append(kwargs)
        return connection

    monkeypatch.setattr(corpus.psycopg, "connect", connect)
    connection.connect_calls = calls
    yield connection
    connection.db.close()


def test_bounded_parameterized_sql_project_isolation_and_read_only(metadata):
    literal = "weird%_file' OR 1=1 --.py"
    metadata.db.execute(
        "insert into gpc_files values (?, ?, ?, ?, ?)",
        ("literal", "p", "a", literal, "literal-hash"),
    )
    case = SearchEvalCase("q", [literal, "foreign.py", "bad.py", "empty.py"])
    snapshot = corpus.load_eval_corpus("ALIAS", [case])
    assert snapshot.project_id == "p"
    assert {row[1] for row in snapshot.files} == {literal, "empty.py"}
    assert all(not row[3] for row in snapshot.files)
    sql, params = metadata.calls[-1]
    assert literal not in sql and "like" not in sql.lower()
    assert params == ("p", sorted(case.expected_paths))
    assert "r.project_id = f.project_id" in sql
    assert "c.project_id = f.project_id" in sql
    assert "coalesce(c.repo_id, f.repo_id) = r.id" in sql
    assert "repeatable read, read only" in metadata.calls[0][0]
    assert "set local statement_timeout" in metadata.calls[1][0]
    assert metadata.connect_calls == [
        {"row_factory": corpus.dict_row, "connect_timeout": 5}
    ]
    assert snapshot.to_dict("f")["matched_files"] == 2
    assert "secret-hash" not in json.dumps(snapshot.to_dict("f"))


def test_project_slug_precedes_conflicting_alias_and_unknown_project_fails(metadata):
    assert corpus.load_eval_corpus("Project", []).project_id == "p"
    with pytest.raises(ProjectResolutionError, match="Unknown project"):
        corpus.load_eval_corpus("missing", [])


def test_chunk_availability_duplicates_and_legacy_ownership(metadata):
    case = SearchEvalCase(
        "q", ["indexed.py", "indexed.py", "legacy.py", "legacy-file.py", "empty.py"]
    )
    snapshot = corpus.load_eval_corpus("project", [case])
    result = snapshot.diagnose_case(case, case.expected_paths, 4)
    assert result["coverage"] == {
        "indexed": 3,
        "file_without_chunks": 1,
        "not_indexed": 0,
        "ambiguous": 0,
        "out_of_scope": 0,
    }
    assert len(snapshot.files) == 4
    assert result["case_index"] == 4
    assert result["not_indexed_expected_paths"] == ["empty.py"]


def test_qualified_exact_unqualified_ambiguous_and_out_of_scope(metadata):
    case = SearchEvalCase("same", ["shared.py", "repo-b:shared.py", "missing.py"])
    snapshot = corpus.load_eval_corpus("project", [case])
    row = snapshot.diagnose_case(case, case.expected_paths, 0)
    assert [label["status"] for label in row["expected_paths"]] == [
        "ambiguous",
        "indexed",
        "not_indexed",
    ]
    scoped = replace(case, repo="Repo A")
    row = snapshot.diagnose_case(scoped, scoped.expected_paths, 1)
    assert [label["status"] for label in row["expected_paths"]] == [
        "indexed",
        "out_of_scope",
        "not_indexed",
    ]
    assert row["indexed_but_not_retrieved_paths"] == ["shared.py"]
    assert row["out_of_scope_expected_paths"] == ["repo-b:shared.py"]


def test_scoped_normalization_empty_list_and_unknown_repo(metadata):
    unscoped = SearchEvalCase("q", ["shared.py"], repo=[])
    scoped = replace(unscoped, repo=["REPO A", "repo-a"])
    snapshot = corpus.load_eval_corpus("project", [unscoped, scoped])
    assert snapshot.diagnose_case(unscoped, [], 0)["coverage"]["ambiguous"] == 1
    assert snapshot.diagnose_case(scoped, [], 1)["coverage"]["indexed"] == 1
    with pytest.raises(ValueError, match="Unknown repositories"):
        corpus.load_eval_corpus("project", [replace(unscoped, repo="missing")])


def test_fingerprint_order_independent_changes_with_hash_repos_and_fixture(metadata):
    snapshot = corpus.load_eval_corpus(
        "project", [SearchEvalCase("q", ["indexed.py", "empty.py"])]
    )
    fingerprint = snapshot.to_dict("fixture-1")["metadata_sha256"]
    assert (
        replace(
            snapshot,
            files=tuple(reversed(snapshot.files)),
            repos=tuple(reversed(snapshot.repos)),
        ).to_dict("fixture-1")["metadata_sha256"]
        == fingerprint
    )
    changed = replace(snapshot, files=(("repo-a", "indexed.py", "changed", True),))
    assert changed.to_dict("fixture-1")["metadata_sha256"] != fingerprint
    assert (
        replace(snapshot, repos=snapshot.repos[:1]).to_dict("fixture-1")[
            "metadata_sha256"
        ]
        != fingerprint
    )
    assert snapshot.to_dict("fixture-2") != snapshot.to_dict("fixture-1")
    report = json.dumps(snapshot.to_dict("fixture-1"))
    assert "POSTGRES_DSN" not in report and "root_path" not in report
    assert "not proof of Qdrant" in report


def test_invalid_repo_normalization_fails_before_connect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected DB call")

    monkeypatch.setattr(corpus.psycopg, "connect", forbidden)
    with pytest.raises(ValueError, match="Invalid slug"):
        corpus.load_eval_corpus("project", [SearchEvalCase("q", [], repo="!!!")])


def test_invalid_search_query_fails_before_connect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected DB call")

    monkeypatch.setattr(corpus.psycopg, "connect", forbidden)
    with pytest.raises(ValueError, match="NUL"):
        corpus.load_eval_corpus("project", [SearchEvalCase("bad\x00query", [])])


def test_metadata_sql_failure_propagates_without_a_snapshot(metadata, monkeypatch):
    execute = metadata.execute

    def fail(sql, params=()):
        if "from gpc_files" in sql:
            raise corpus.psycopg.OperationalError("metadata read failed")
        return execute(sql, params)

    monkeypatch.setattr(metadata, "execute", fail)
    with pytest.raises(corpus.psycopg.OperationalError, match="metadata read failed"):
        corpus.load_eval_corpus("project", [SearchEvalCase("q", ["indexed.py"])])
