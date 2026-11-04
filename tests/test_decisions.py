"""Real decision workflows on isolated SQLite with narrow SQL adaptation.

This checks transitions, SQL contracts, constraints and rollback side effects,
not PostgreSQL engine semantics or actual concurrent PostgreSQL execution.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg.errors import UniqueViolation
import pytest

from gpc import decisions
from gpc.registry import ProjectResolutionError

MIGRATION = Path(__file__).parents[1] / "migrations/0011_decision_memory.sql"


def ident(name):
    return str(uuid5(NAMESPACE_URL, name))


class IsolatedDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row, strict=True)
        )
        self.clock = 0
        self.conn.create_function("now", 0, self.now)
        self.conn.create_function("gen_random_uuid", 0, lambda: str(uuid4()))
        self.statements = []
        self.transactions = []
        self.resolutions = []
        self.conn.executescript("""
            pragma foreign_keys = on;
            create table gpc_projects (id text primary key, slug text);
            create table gpc_repos (
                id text primary key, project_id text references gpc_projects(id),
                slug text, unique(project_id, slug)
            );
            create table gpc_files (
                id text primary key, project_id text references gpc_projects(id),
                repo_id text references gpc_repos(id), relative_path text,
                content_hash text, indexed_at text,
                unique(project_id, repo_id, relative_path)
            );
            -- SQLite cannot ALTER COLUMN DEFAULT; model the final pending
            -- default here, and assert the PostgreSQL ALTER separately below.
            create table gpc_decisions (
                id text primary key, project_id text references gpc_projects(id),
                title text not null, status text not null default 'pending',
                content text not null, source_path text, decided_at text,
                metadata text not null default '{}',
                created_at text not null default (now()),
                updated_at text not null default (now())
            );
            """)
        for project in ("p", "q"):
            self.conn.execute(
                "insert into gpc_projects values (?, ?)", (ident(project), project)
            )
            for repo in ("a", "b"):
                self.conn.execute(
                    "insert into gpc_repos values (?, ?, ?)",
                    (ident(project + repo), ident(project), repo),
                )
                self.add_file(project, repo)
        for name, project in (("legacy", ident("p")), ("global", None)):
            self.conn.execute(
                """insert into gpc_decisions
                   (id, project_id, title, content, status, source_path)
                   values (?, ?, 'Original title', 'Original content', 'active', 'ADR.md')""",
                (ident(name), project),
            )
        self.apply_migration()
        self.conn.commit()

    def now(self):
        self.clock += 1
        return f"2026-11-04T00:00:{self.clock:06d}Z"

    def apply_migration(self):
        for statement in MIGRATION.read_text().split(";"):
            statement = re.sub(r"--[^\n]*", "", statement).strip()
            if not statement:
                continue
            if "alter column status" in statement:
                continue  # Contract assertion below; unsupported by SQLite.
            statement = re.sub(r"\buuid\b|\btimestamptz\b", "text", statement)
            statement = statement.replace("default now()", "default (now())")
            statement = statement.replace(
                "default gen_random_uuid()", "default (gen_random_uuid())"
            )
            if statement.startswith("alter table gpc_decisions"):
                for column in statement.split("\n", 1)[1].split(",\n"):
                    self.conn.execute(
                        "alter table gpc_decisions "
                        + column.strip().replace(
                            "add column if not exists", "add column"
                        )
                    )
            else:
                self.conn.execute(statement)

    def add_file(self, project="p", repo="a", path="ADR.md", hash_value=None):
        file_id = str(uuid4())
        self.conn.execute(
            "insert into gpc_files values (?, ?, ?, ?, ?, ?)",
            (
                file_id,
                ident(project),
                ident(project + repo),
                path,
                hash_value or f"hash-{project}-{repo}",
                self.now(),
            ),
        )
        return file_id

    @contextmanager
    def connect(self, **kwargs):
        statements = []
        self.transactions.append(statements)
        self.active = statements
        with self.conn:
            yield self

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        self.active.append(sql)
        adapted = re.sub(r"\bfor (update|share)\b", "", sql, flags=re.I)
        adapted = adapted.replace("%s", "?")
        return self.conn.execute(
            adapted, tuple(str(p) if isinstance(p, UUID) else p for p in params)
        )

    def resolve(self, *, project, repo):
        self.resolutions.append((project, repo))
        p = self.conn.execute(
            "select * from gpc_projects where slug = ?", (project,)
        ).fetchone()
        r = None
        if p and repo is not None:
            r = self.conn.execute(
                "select * from gpc_repos where project_id = ? and slug = ?",
                (p["id"], repo),
            ).fetchone()
        if not p or (repo is not None and not r):
            raise ProjectResolutionError("Unknown explicit project/repo")
        return {"project": p, "repo": r}

    def snapshot(self):
        return {
            name: self.conn.execute(f"select * from {name} order by id").fetchall()
            for name in ("gpc_decisions", "gpc_decision_events")
        }


@pytest.fixture
def env(monkeypatch):
    db = IsolatedDatabase()
    monkeypatch.setattr(decisions, "pg_connection", db.connect)
    monkeypatch.setattr(decisions, "resolve_repo", db.resolve)

    def capture(**overrides):
        values = dict(
            project="p",
            repo="a",
            decision_key="storage",
            title="Use local storage",
            content="Explicitly selected local storage for this component.",
            source_path="ADR.md",
            actor="human",
        )
        return decisions.capture_decision(**(values | overrides))

    yield SimpleNamespace(db=db, capture=capture)
    db.conn.close()


def approve(row, **overrides):
    return decisions.approve_decision(
        row["id"], **(dict(project="p", repo="a", actor="reviewer") | overrides)
    )


def diagnostic(row, **overrides):
    return decisions.get_decision(
        row["id"], **(dict(project="p", repo="a") | overrides)
    )


def test_schema_contract_and_legacy_are_not_backfilled_or_approved(env):
    sql = " ".join(MIGRATION.read_text().lower().split())
    assert "alter column status set default 'pending'" in sql
    assert "set status = 'legacy' where status = 'active'" in sql
    assert (
        "on gpc_decisions(project_id, repo_id, source_path, decision_key) where status = 'approved'"
        in sql
    )
    assert "source_file_id uuid references gpc_files(id) on delete set null" in sql
    assert "superseded_by uuid references gpc_decisions(id) on delete set null" in sql
    assert (
        "decision_id uuid not null references gpc_decisions(id) on delete cascade"
        in sql
    )
    legacy = env.db.conn.execute(
        "select * from gpc_decisions where id = ?", (ident("legacy"),)
    ).fetchone()
    assert legacy["status"] == "legacy"
    assert legacy["title"] == "Original title"
    assert legacy["content"] == "Original content"
    assert all(
        legacy[field] is None
        for field in ("repo_id", "source_hash", "approved_by", "decision_key")
    )
    assert decisions.list_decisions(project="p", include_inactive=True) == []
    for name in ("legacy", "global"):
        with pytest.raises(decisions.DecisionError, match="not found"):
            decisions.get_decision(ident(name), project="p")


def test_pending_capture_fresh_approval_and_read_only_default_retrieval(env):
    pending = env.capture(
        source_path="./ADR.md", source_revision="human milestone label"
    )
    assert pending["status"] == "pending"
    assert pending["freshness"] == "current"
    assert pending["source_path"] == "ADR.md"
    assert pending["source_hash"] == "hash-p-a"
    assert pending["source_revision"] == "human milestone label"
    assert pending["repo_slug"] == "a"
    assert pending["key"] == pending["decision_key"] == "storage"
    assert pending["approved_by"] is None
    assert [event["action"] for event in pending["events"]] == ["captured"]
    assert pending["events"][0]["actor"] == "human"
    assert decisions.list_decisions(project="p") == []
    assert (
        decisions.list_decisions(project="p", include_inactive=True)[0]["id"]
        == pending["id"]
    )
    approved = approve(pending, reason="Reviewed against indexed ADR")
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "reviewer"
    assert approved["approval_reason"] == "Reviewed against indexed ADR"
    assert approved["approved_at"] and approved["decided_at"]
    assert [e["action"] for e in approved["events"]] == ["captured", "approved"]
    before = env.db.snapshot()
    assert diagnostic(pending) == approved
    assert decisions.list_decisions(project="p", repo="a")[0]["id"] == pending["id"]
    assert env.db.snapshot() == before
    json.dumps(approved)
    with pytest.raises(decisions.DecisionError, match="Only pending"):
        approve(pending)
    assert env.db.snapshot() == before


@pytest.mark.parametrize(
    ("mutation", "freshness"),
    [
        ("hash", "stale"),
        ("delete", "missing_source"),
        ("replace", "unverified"),
        ("unindexed", "unverified"),
        ("empty_hash", "unverified"),
        ("foreign_project", "missing_source"),
        ("foreign_repo", "missing_source"),
        ("foreign_snapshot", "stale"),
    ],
)
def test_changed_missing_or_foreign_evidence_never_endorsed_and_reads_do_not_write(
    env, mutation, freshness
):
    approved = approve(env.capture())
    pending = env.capture(decision_key="other")
    db = env.db.conn
    file_id = approved["source_file_id"]
    if mutation == "hash":
        db.execute(
            "update gpc_files set content_hash = 'changed' where id = ?", (file_id,)
        )
    elif mutation in {"delete", "replace"}:
        db.execute("delete from gpc_files where id = ?", (file_id,))
        if mutation == "replace":
            env.db.add_file(hash_value=approved["source_hash"])
    elif mutation == "unindexed":
        db.execute("update gpc_files set indexed_at = null where id = ?", (file_id,))
    elif mutation == "empty_hash":
        db.execute("update gpc_files set content_hash = ' ' where id = ?", (file_id,))
    elif mutation == "foreign_project":
        db.execute(
            "update gpc_files set project_id = ? where id = ?", (ident("q"), file_id)
        )
    elif mutation == "foreign_repo":
        foreign = env.db.add_file(project="q", repo="b", path="other.md")
        db.execute(
            "update gpc_files set repo_id = ? where id = ?", (ident("qb"), file_id)
        )
        assert foreign
    else:
        foreign = db.execute(
            "select id from gpc_files where project_id = ? limit 1", (ident("q"),)
        ).fetchone()["id"]
        db.execute("update gpc_decisions set source_file_id = ?", (foreign,))
    db.commit()
    before = env.db.snapshot()
    start = len(env.db.statements)
    assert decisions.list_decisions(project="p") == []
    assert diagnostic(approved)["freshness"] == freshness
    assert {
        row["freshness"]
        for row in decisions.list_decisions(project="p", include_inactive=True)
    } == {freshness}
    assert all(
        sql.lstrip().startswith("select") for sql, _ in env.db.statements[start:]
    )
    assert env.db.snapshot() == before
    with pytest.raises(decisions.DecisionError, match="evidence|stale"):
        approve(pending)
    assert env.db.snapshot() == before


@pytest.mark.parametrize("missing", ["no_file", "hash", "indexed_at"])
def test_capture_requires_indexed_evidence(env, missing):
    if missing == "no_file":
        env.db.conn.execute("delete from gpc_files where repo_id = ?", (ident("pa"),))
    else:
        column = "content_hash" if missing == "hash" else "indexed_at"
        env.db.conn.execute(
            f"update gpc_files set {column} = null where repo_id = ?", (ident("pa"),)
        )
    env.db.conn.commit()
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError, match="indexed hash evidence"):
        env.capture()
    assert env.db.snapshot() == before


@pytest.mark.parametrize("initial", ["pending", "approved"])
def test_invalidate_requires_reason_and_is_terminal(env, initial):
    row = env.capture()
    if initial == "approved":
        row = approve(row)
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError, match="reason"):
        decisions.invalidate_decision(
            row["id"], project="p", repo="a", actor="human", reason=" "
        )
    assert env.db.snapshot() == before
    result = decisions.invalidate_decision(
        row["id"], project="p", repo="a", actor="human", reason="No longer appropriate"
    )
    assert result["status"] == "invalidated"
    assert result["events"][-1]["action"] == "invalidated"
    assert result["events"][-1]["reason"] == "No longer appropriate"
    assert decisions.list_decisions(project="p") == []
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError):
        approve(row)
    with pytest.raises(decisions.DecisionError):
        decisions.invalidate_decision(
            row["id"], project="p", repo="a", actor="human", reason="Again"
        )
    assert env.db.snapshot() == before


def test_key_conflict_and_explicit_supersede_are_atomic_with_both_events(env):
    old = approve(env.capture())
    new = env.capture(content="Select a different explicit proposition")
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionConflictError, match="supersede ID"):
        approve(new)
    assert env.db.snapshot() == before
    result = approve(new, supersede=old["id"], reason="Human replacement")
    prior = diagnostic(old)
    assert prior["status"] == "superseded"
    assert prior["superseded_by"] == new["id"]
    assert prior["content"] == old["content"]
    assert result["content"] == new["content"]
    assert prior["events"][-1]["action"] == "superseded"
    assert prior["events"][-1]["actor"] == result["events"][-1]["actor"] == "reviewer"
    assert (
        prior["events"][-1]["reason"]
        == result["events"][-1]["reason"]
        == "Human replacement"
    )
    assert [r["id"] for r in decisions.list_decisions(project="p")] == [new["id"]]
    with pytest.raises(decisions.DecisionError, match="Only pending"):
        approve(old)
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError):
        decisions.invalidate_decision(
            old["id"], project="p", repo="a", actor="h", reason="Already superseded"
        )
    assert env.db.snapshot() == before


@pytest.mark.parametrize(
    "wrong", ["key", "path", "pending", "self", "missing", "repo", "project"]
)
def test_supersede_rejects_anything_other_than_exact_approved_scoped_key(env, wrong):
    old = approve(env.capture())
    new = env.capture()
    if wrong == "key":
        candidate = approve(env.capture(decision_key="other"))
    elif wrong == "path":
        env.db.add_file(path="other.md")
        env.db.conn.commit()
        candidate = approve(env.capture(source_path="other.md"))
    elif wrong in {"repo", "project"}:
        project, repo = ("p", "b") if wrong == "repo" else ("q", "a")
        candidate = approve(
            env.capture(project=project, repo=repo), project=project, repo=repo
        )
    else:
        candidate = {"id": str(uuid4())} if wrong == "missing" else new
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError):
        approve(new, supersede=candidate["id"])
    assert env.db.snapshot() == before
    assert diagnostic(old)["status"] == "approved"


@pytest.mark.parametrize("failure", ["event", "unique"])
def test_supersede_rolls_back_old_new_and_both_events_on_failure(
    env, monkeypatch, failure
):
    old = approve(env.capture())
    new = env.capture()
    before = env.db.snapshot()
    if failure == "event":
        original = decisions._event

        def fail(conn, decision_id, action, actor, reason):
            if action == "approved":
                raise RuntimeError("audit unavailable")
            original(conn, decision_id, action, actor, reason)

        monkeypatch.setattr(decisions, "_event", fail)
        error = RuntimeError
    else:
        original = env.db.execute

        def fail(sql, params=()):
            if "set status = 'approved'" in sql:
                raise UniqueViolation("noncooperating writer won")
            return original(sql, params)

        monkeypatch.setattr(env.db, "execute", fail)
        error = decisions.DecisionConflictError
    with pytest.raises(error):
        approve(new, supersede=old["id"])
    assert env.db.snapshot() == before


@pytest.mark.parametrize("operation", ["capture", "invalidate"])
def test_audit_failure_rolls_back_transition(env, monkeypatch, operation):
    row = env.capture()
    before = env.db.snapshot()

    def fail(*args):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(decisions, "_event", fail)
    with pytest.raises(RuntimeError):
        if operation == "capture":
            env.capture(decision_key="other")
        else:
            decisions.invalidate_decision(
                row["id"], project="p", repo="a", actor="h", reason="Retired"
            )
    assert env.db.snapshot() == before


@pytest.mark.parametrize(("project", "repo"), [("p", "b"), ("q", "a")])
def test_scope_isolation_for_all_crud_and_project_only_reads(env, project, repo):
    original = approve(env.capture())
    other = approve(env.capture(project=project, repo=repo), project=project, repo=repo)
    assert original["source_file_id"] != other["source_file_id"]
    assert original["source_hash"] != other["source_hash"]
    before = env.db.snapshot()
    for action in ("get", "approve", "invalidate"):
        with pytest.raises(decisions.DecisionError, match="not found"):
            if action == "get":
                decisions.get_decision(original["id"], project=project, repo=repo)
            elif action == "approve":
                approve(original, project=project, repo=repo)
            else:
                decisions.invalidate_decision(
                    original["id"],
                    project=project,
                    repo=repo,
                    actor="human",
                    reason="Injected ID",
                )
    assert env.db.snapshot() == before
    assert [r["id"] for r in decisions.list_decisions(project=project, repo=repo)] == [
        other["id"]
    ]
    assert other["id"] in {r["id"] for r in decisions.list_decisions(project=project)}
    if project == "q":
        with pytest.raises(decisions.DecisionError):
            decisions.get_decision(original["id"], project=project)
        assert original["id"] not in {
            r["id"] for r in decisions.list_decisions(project=project)
        }


def test_foreign_repo_project_membership_and_unregistered_scope_fail_closed(
    env, monkeypatch
):
    for overrides in ({"project": "unknown"}, {"repo": "unknown"}, {"repo": None}):
        with pytest.raises(decisions.DecisionError):
            env.capture(**overrides)
    resolved = env.db.resolve(project="p", repo="a")
    resolved["repo"]["project_id"] = ident("q")
    monkeypatch.setattr(decisions, "resolve_repo", lambda **kwargs: resolved)
    with pytest.raises(decisions.DecisionError, match="belong"):
        env.capture()
    assert env.db.transactions == []


def test_hydration_excludes_decision_whose_repo_belongs_to_another_project(env):
    row = env.capture()
    env.db.conn.execute(
        "update gpc_decisions set repo_id = ? where id = ?", (ident("qa"), row["id"])
    )
    env.db.conn.commit()
    assert decisions.list_decisions(project="p", include_inactive=True) == []
    with pytest.raises(decisions.DecisionError, match="not found"):
        diagnostic(row)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("decision_key", "UPPER"),
        ("decision_key", "a.b"),
        ("decision_key", "a" * 81),
        ("title", "t" * 241),
        ("title", " "),
        ("content", "c" * 8001),
        ("actor", "a" * 129),
        ("source_revision", "r" * 129),
        ("source_path", "/absolute"),
        ("source_path", "../ADR.md"),
        ("source_path", "docs/../ADR.md"),
        ("source_path", "a\\b"),
        ("source_path", "C:/ADR.md"),
        ("source_path", "\x00bad"),
        ("source_path", "."),
        ("source_path", ""),
        ("source_path", "p" * 513),
        ("project", ""),
        ("repo", ""),
        ("content", None),
    ],
)
def test_capture_validation_precedes_resolution_and_connection(env, name, value):
    with pytest.raises(decisions.DecisionError):
        env.capture(**{name: value})
    assert env.db.resolutions == []
    assert env.db.transactions == []


@pytest.mark.parametrize("field", ["title", "content", "source_revision", "actor"])
def test_capture_rejects_indexer_recognized_secrets_without_backend(env, field):
    with pytest.raises(decisions.DecisionError, match="sensitive"):
        env.capture(**{field: "-----BEGIN PRIVATE KEY-----"})
    assert env.db.resolutions == []
    assert env.db.transactions == []


@pytest.mark.parametrize("operation", ["approve", "invalidate"])
@pytest.mark.parametrize(
    "reason", ["r" * 2001, "-----BEGIN PRIVATE KEY-----", "\x00bad", ""]
)
def test_reason_validation_before_backend(env, operation, reason):
    method = (
        decisions.approve_decision
        if operation == "approve"
        else decisions.invalidate_decision
    )
    with pytest.raises(decisions.DecisionError):
        method(str(uuid4()), project="p", repo="a", actor="human", reason=reason)
    assert env.db.resolutions == []
    assert env.db.transactions == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.0},
        {"limit": float("inf")},
        {"limit": None},
        {"query": "q" * 513},
        {"query": ""},
        {"query": "\x00bad"},
        {"path": "../bad"},
        {"include_inactive": "yes"},
    ],
)
def test_list_validation_before_backend(env, kwargs):
    with pytest.raises(decisions.DecisionError):
        decisions.list_decisions(project="p", **kwargs)
    assert env.db.resolutions == []
    assert env.db.transactions == []


def test_id_and_supersede_validation_before_backend(env):
    for method in (
        decisions.get_decision,
        decisions.approve_decision,
        decisions.invalidate_decision,
    ):
        kwargs = {} if method is decisions.get_decision else {"repo": "a", "actor": "h"}
        if method is decisions.invalidate_decision:
            kwargs["reason"] = "Retire"
        with pytest.raises(decisions.DecisionError, match="UUID"):
            method("not-a-uuid", project="p", **kwargs)
    with pytest.raises(decisions.DecisionError, match="UUID"):
        decisions.approve_decision(
            str(uuid4()), project="p", repo="a", actor="h", supersede="bad"
        )
    assert env.db.resolutions == []
    assert env.db.transactions == []


def test_literal_query_and_path_are_parameterized_with_limits(env):
    special_path = "docs/100%_! O'Reilly.md"
    env.db.add_file(path=special_path)
    env.db.conn.commit()
    special = approve(
        env.capture(
            title="100%_! O'Reilly",
            content="literal ' OR 1=1 -- marker",
            source_path=special_path,
        )
    )
    ordinary = approve(
        env.capture(decision_key="ordinary", title="ordinary", content="ordinary")
    )
    for query in ("%", "_", "!", "O'Reilly", "' OR 1=1 --", "STORAGE"):
        rows = decisions.list_decisions(project="p", query=query)
        assert [r["id"] for r in rows] == [special["id"]]
    sql, params = env.db.statements[-1]
    assert "STORAGE" not in sql and "%STORAGE%" in params
    assert [
        r["id"] for r in decisions.list_decisions(project="p", path=special_path)
    ] == [special["id"]]
    sql, params = env.db.statements[-1]
    assert special_path not in sql and special_path in params
    assert len(decisions.list_decisions(project="p", limit=1)) == 1
    assert len(decisions.list_decisions(project="p", limit=100)) == 2
    assert ordinary["id"] != special["id"]


def test_stable_repo_lock_first_and_unique_index_protect_approval_contract(env):
    first = env.capture()
    second = env.capture()
    approve(first)
    decisions.invalidate_decision(
        first["id"], project="p", repo="a", actor="h", reason="Retired"
    )
    approve(second)
    third = env.capture()
    for transaction in env.db.transactions:
        assert transaction[0].strip().startswith("select id from gpc_repos")
        assert transaction[0].endswith("for update")
    assert any(
        "for share" in sql and "gpc_files" in sql for sql, _ in env.db.statements
    )
    assert any(
        "for update" in sql and "select * from gpc_decisions" in sql
        for sql, _ in env.db.statements
    )
    # Exercise the actual adapted partial index, independently of API locking.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with env.db.conn:
            env.db.conn.execute(
                "update gpc_decisions set status = 'approved' where id = ?",
                (third["id"],),
            )
    assert diagnostic(third)["status"] == "pending"


def test_repo_membership_rechecked_inside_write_transaction(env, monkeypatch):
    resolved = env.db.resolve(project="p", repo="a")
    env.db.conn.execute("delete from gpc_files where repo_id = ?", (ident("pa"),))
    env.db.conn.execute("delete from gpc_repos where id = ?", (ident("pa"),))
    env.db.conn.commit()
    monkeypatch.setattr(decisions, "resolve_repo", lambda **kwargs: resolved)
    with pytest.raises(decisions.DecisionError, match="no longer belongs"):
        env.capture()
    assert len(env.db.transactions[0]) == 1


def test_event_cascade_and_source_fk_set_null(env):
    row = env.capture()
    env.db.conn.execute("delete from gpc_files where id = ?", (row["source_file_id"],))
    env.db.conn.commit()
    assert diagnostic(row)["source_file_id"] is None
    env.db.conn.execute("delete from gpc_decisions where id = ?", (row["id"],))
    assert env.db.conn.execute("select * from gpc_decision_events").fetchall() == []


def test_json_safe_uuid_and_timestamp_serialization():
    value = {"id": uuid4(), "date": datetime(2026, 11, 4, tzinfo=timezone.utc)}
    result = decisions._json(value)
    assert isinstance(result["id"], str)
    assert result["date"] == "2026-11-04T00:00:00+00:00"
    json.dumps(result)


def test_documented_size_boundaries_are_accepted_without_source_file_reads(
    env, monkeypatch
):
    path = "p" * 512
    env.db.add_file(path=path)
    env.db.conn.commit()

    def forbid_reads(*args, **kwargs):
        raise AssertionError("Decision memory must only use indexed evidence")

    monkeypatch.setattr(Path, "read_text", forbid_reads)
    monkeypatch.setattr(Path, "read_bytes", forbid_reads)
    monkeypatch.setattr(Path, "open", forbid_reads)
    row = env.capture(
        decision_key="k" * 80,
        title="t" * 240,
        content="c" * 8000,
        actor="a" * 128,
        source_revision="r" * 128,
        source_path=path,
    )
    assert approve(row, reason="r" * 2000)["status"] == "approved"
    assert decisions.list_decisions(project="p", query="q" * 512) == []


def test_scoped_legacy_with_no_snapshot_is_unverified_and_not_approvable(env):
    env.db.conn.execute(
        "update gpc_decisions set repo_id = ? where id = ?",
        (ident("pa"), ident("legacy")),
    )
    env.db.conn.commit()
    row = decisions.get_decision(ident("legacy"), project="p", repo="a")
    assert row["status"] == "legacy"
    assert row["freshness"] == "unverified"
    assert row["events"] == []
    assert decisions.list_decisions(project="p") == []
    before = env.db.snapshot()
    with pytest.raises(decisions.DecisionError, match="Only pending"):
        approve(row)
    assert env.db.snapshot() == before
