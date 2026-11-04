from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from psycopg.types.json import Jsonb
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from gpc import indexer


class IsolatedDatabase:
    """Execute the real indexer SQL on SQLite with narrow dialect adaptation."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row, strict=True)
        )
        self.conn.create_function("now", 0, lambda: "completed")
        self.conn.executescript("""
            pragma foreign_keys = on;
            create table gpc_files (
                id text primary key default (lower(hex(randomblob(16)))),
                project_id text, repo_id text, relative_path text,
                absolute_path text, language text, file_type text,
                size_bytes integer, content_hash text, metadata text,
                indexed_at text
            );
            create unique index file_identity on gpc_files (
                project_id,
                coalesce(repo_id, '00000000-0000-0000-0000-000000000000'),
                relative_path
            );
            create table gpc_chunks (
                id text primary key, project_id text, repo_id text,
                file_id text references gpc_files(id) on delete cascade,
                source_type text, chunk_type text, chunk_index integer,
                title text, content text, content_hash text, token_count integer,
                qdrant_collection text, qdrant_point_id text,
                embedding_model text, metadata text
            );
            """)

    @contextmanager
    def connect(self):
        with self.conn:
            yield self

    def transaction(self):
        return self.conn

    def execute(self, query, params=()):
        query = query.replace("::uuid", "").replace("%s", "?")
        query = query.replace("is not distinct from", "is")
        if "any(?::text[])" in query:
            paths = params[-1]
            query = query.replace(
                "relative_path = any(?::text[])",
                f"relative_path in ({','.join('?' for _ in paths)})",
            )
            params = (*params[:-1], *paths)
        values = tuple(
            (
                json.dumps(value.obj)
                if isinstance(value, Jsonb)
                else str(value) if isinstance(value, UUID) else value
            )
            for value in params
        )
        return self.conn.execute(query, values)

    def files(self):
        return [dict(row) for row in self.conn.execute("select * from gpc_files")]

    def point_ids(self, file_id=None):
        query = "select qdrant_point_id from gpc_chunks"
        params = ()
        if file_id is not None:
            query += " where file_id = ?"
            params = (file_id,)
        return {row["qdrant_point_id"] for row in self.conn.execute(query, params)}


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    db = IsolatedDatabase()
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection(
        indexer.COLLECTION_NAME,
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    calls = []

    def embed(texts):
        calls.append(texts)
        return SimpleNamespace(
            vectors=[[1.0, 0.5] for _ in texts], provider="test", model="test"
        )

    monkeypatch.setattr(indexer, "_connect", db.connect)
    monkeypatch.setattr(indexer, "embed_texts", embed)

    def project(repo):
        return {
            "id": str(uuid5(NAMESPACE_URL, "project")),
            "slug": "project",
            "name": "Project",
            "root_path": str(tmp_path / (repo or "legacy")),
            "_repo_id": str(uuid5(NAMESPACE_URL, repo)) if repo else None,
            "_repo_slug": repo,
        }

    def candidate(repo, text="same content"):
        path = tmp_path / (repo or "legacy") / "README.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return indexer.FileCandidate(
            path, "README.md", path.stat().st_size, "markdown", "documentation"
        )

    def points():
        return {
            str(point.id)
            for point in qdrant.scroll(indexer.COLLECTION_NAME, limit=1000)[0]
        }

    yield SimpleNamespace(
        db=db,
        qdrant=qdrant,
        calls=calls,
        project=project,
        candidate=candidate,
        points=points,
    )
    qdrant.close()
    db.conn.close()


def run_index(env, repo, text="same content", **options):
    return indexer._index_file(
        env.project(repo),
        env.candidate(repo, text),
        env.qdrant,
        indexer.IndexOptions(**options),
    )


def test_same_path_and_content_index_separately_and_noop_avoids_embedding(lifecycle):
    env = lifecycle
    assert run_index(env, "a").status == "indexed"
    a_points = env.points()
    assert run_index(env, "b").status == "indexed"
    assert len(env.db.files()) == 2
    assert len(env.points()) == 2
    assert len(a_points & env.points()) == 1
    assert run_index(env, "b").status == "unchanged"
    assert len(env.calls) == 2
    assert env.points() == env.db.point_ids()


@pytest.mark.parametrize("repo", ["b", None])
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty_text"),
        ("\x00binary", "binary_content"),
        ("-----BEGIN PRIVATE KEY-----", "sensitive_content"),
    ],
)
def test_removing_file_is_repo_scoped(lifecycle, repo, text, reason):
    env = lifecycle
    run_index(env, "a")
    retained = env.points()
    run_index(env, repo)
    result = run_index(env, repo, text)
    assert result.status == "skipped"
    assert result.reason == reason
    assert result.points_deleted == 1
    assert env.points() == retained
    assert [row["repo_id"] for row in env.db.files()] == [env.project("a")["_repo_id"]]


@pytest.mark.parametrize("repo", ["b", None])
@pytest.mark.parametrize("operation", ["reset", "prune"])
def test_reset_and_prune_only_remove_selected_repo(lifecycle, repo, operation):
    env = lifecycle
    for name in ("a", "b", None):
        run_index(env, name)
    target = next(
        row for row in env.db.files() if row["repo_id"] == env.project(repo)["_repo_id"]
    )
    retained = env.points() - env.db.point_ids(target["id"])
    if operation == "reset":
        indexer._clear_project_index(env.project(repo), env.qdrant)
    else:
        indexer._prune_missing_files(env.project(repo), set(), env.qdrant)
    assert env.points() == retained
    assert len(env.db.files()) == 2
    assert all(row["repo_id"] != target["repo_id"] for row in env.db.files())


def fail_once_after_partial_write(monkeypatch, qdrant, operation):
    original = getattr(qdrant, operation)
    failed = False

    def write(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            key = "points" if operation == "upsert" else "points_selector"
            partial = dict(kwargs)
            partial[key] = kwargs[key][:1]
            original(**partial)
            raise RuntimeError("injected partial projection failure")
        return original(**kwargs)

    monkeypatch.setattr(qdrant, operation, write)


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_retry_reconciles_forgotten_old_ids_after_partial_failure(
    lifecycle, monkeypatch, operation
):
    env = lifecycle
    run_index(env, "a")
    retained = env.points()
    run_index(env, "b", "old content with several chunks", chunk_chars=8)
    old_ids = env.points() - retained
    fail_once_after_partial_write(monkeypatch, env.qdrant, operation)
    with pytest.raises(RuntimeError, match="injected partial"):
        run_index(env, "b", "new content", chunk_chars=8)
    b_row = next(
        row for row in env.db.files() if row["repo_id"] == env.project("b")["_repo_id"]
    )
    assert b_row["indexed_at"] is None
    assert old_ids - env.db.point_ids()
    assert run_index(env, "b", "new content", chunk_chars=8).status == "indexed"
    assert env.points() == env.db.point_ids()
    assert retained <= env.points()
    assert not (old_ids & env.points())
    calls = len(env.calls)
    assert run_index(env, "b", "new content", chunk_chars=8).status == "unchanged"
    assert len(env.calls) == calls


def test_ordinary_pass_restores_vectors_after_collection_recreation(lifecycle):
    env = lifecycle
    run_index(env, "b")
    expected = env.points()
    env.qdrant.delete_collection(indexer.COLLECTION_NAME)
    env.qdrant.create_collection(
        indexer.COLLECTION_NAME,
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    assert run_index(env, "b").status == "indexed"
    assert env.points() == expected
    assert len(env.calls) == 2


@pytest.mark.parametrize("operation", ["remove", "prune"])
def test_removal_also_cleans_orphans_from_failed_replacement(
    lifecycle, monkeypatch, operation
):
    env = lifecycle
    run_index(env, "b", "old content with several chunks", chunk_chars=8)
    fail_once_after_partial_write(monkeypatch, env.qdrant, "upsert")
    with pytest.raises(RuntimeError):
        run_index(env, "b", "new content")
    if operation == "remove":
        run_index(env, "b", "")
    else:
        indexer._prune_missing_files(env.project("b"), set(), env.qdrant)
    assert not env.points()
    assert not env.db.files()


def test_projection_inventory_paginates_and_detects_partial_vector_loss(lifecycle):
    env = lifecycle
    text = "x" * 300
    run_index(env, "b", text, chunk_chars=1)
    expected = env.points()
    assert len(expected) == 300
    assert run_index(env, "b", text, chunk_chars=1).status == "unchanged"
    env.qdrant.delete(
        indexer.COLLECTION_NAME, points_selector=[next(iter(expected))], wait=True
    )
    assert run_index(env, "b", text, chunk_chars=1).status == "indexed"
    assert env.points() == expected


def test_file_replacement_retains_other_files_and_projects(lifecycle):
    env = lifecycle
    candidate = env.candidate("a")
    other_path = candidate.absolute_path.with_name("other.md")
    other_path.write_text("other content")
    other = indexer.FileCandidate(
        other_path, "other.md", other_path.stat().st_size, "markdown", "documentation"
    )
    indexer._index_file(env.project("a"), other, env.qdrant, indexer.IndexOptions())
    other_project = {
        **env.project("a"),
        "id": str(uuid5(NAMESPACE_URL, "other-project")),
    }
    indexer._index_file(other_project, candidate, env.qdrant, indexer.IndexOptions())
    retained = env.points()
    run_index(env, "a", "first content")
    assert run_index(env, "a", "first content").status == "unchanged"
    run_index(env, "a", "replacement content")
    assert retained <= env.points()
    assert env.points() == env.db.point_ids()
    run_index(env, "a", "")
    assert env.points() == retained


def test_failed_removal_keeps_sql_identity_for_retry(lifecycle, monkeypatch):
    env = lifecycle
    run_index(env, "a")
    retained = env.points()
    run_index(env, "b", "many chunks to delete", chunk_chars=4)
    fail_once_after_partial_write(monkeypatch, env.qdrant, "delete")
    with pytest.raises(RuntimeError, match="injected partial"):
        run_index(env, "b", "")
    assert len(env.db.files()) == 2
    assert run_index(env, "b", "").status == "skipped"
    assert len(env.db.files()) == 1
    assert env.points() == retained
