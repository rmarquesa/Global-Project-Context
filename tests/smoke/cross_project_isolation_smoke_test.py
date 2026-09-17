"""Dedicated smoke test for cross-project isolation across read surfaces.

Creates two synthetic projects with one repo each and seeds distinctive data in
Postgres, Qdrant, and Neo4j. It then verifies that semantic search, context
composition, context packs, and graph queries never leak content or nodes from
one project into the other.

Always cleans up seeded data so the test can be re-run safely.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from gpc.config import COLLECTION_NAME, QDRANT_HOST, QDRANT_PORT
from gpc.context_pack import build_context_pack
from gpc.db import pg_connection
from gpc.embeddings import embed_texts
from gpc.graph import neo4j_driver
from gpc.graph_query import graph_neighbors, graph_path, graph_summary
from gpc.registry import register_project, register_repo
from gpc.search import compose_project_context, search_project_context


ALPHA_SLUG = "isolation-alpha"
BETA_SLUG = "isolation-beta"
FIXTURE_ROOT = Path(__file__).resolve().parent / ".cross-project-isolation-fixture"


PROJECTS = {
    ALPHA_SLUG: {
        "name": "Isolation Alpha",
        "repo_slug": "alpha-repo",
        "secret": "ALPHA_SECRET_TOKEN",
        "relative_path": "src/alpha_secret.py",
        "source_id_entry": "alpha_entry",
        "source_id_secret": "alpha_secret",
        "entry_label": "AlphaEntry.run()",
        "secret_label": "AlphaVault.reveal()",
        "entry_text": (
            "Alpha entrypoint routes requests to the alpha vault. "
            "Only Isolation Alpha knows ALPHA_SECRET_TOKEN."
        ),
        "secret_text": (
            "Isolation Alpha private context. Do not leak ALPHA_SECRET_TOKEN "
            "outside project isolation-alpha."
        ),
    },
    BETA_SLUG: {
        "name": "Isolation Beta",
        "repo_slug": "beta-repo",
        "secret": "BETA_SECRET_TOKEN",
        "relative_path": "src/beta_secret.py",
        "source_id_entry": "beta_entry",
        "source_id_secret": "beta_secret",
        "entry_label": "BetaEntry.run()",
        "secret_label": "BetaVault.reveal()",
        "entry_text": (
            "Beta entrypoint routes requests to the beta vault. "
            "Only Isolation Beta knows BETA_SECRET_TOKEN."
        ),
        "secret_text": (
            "Isolation Beta private context. Do not leak BETA_SECRET_TOKEN "
            "outside project isolation-beta."
        ),
    },
}


def _uid(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "/".join(parts)))


def _graph_id(project_slug: str, repo_slug: str, source_id: str) -> str:
    return f"{project_slug}:{repo_slug}:{source_id}"


def _project_dir(slug: str) -> Path:
    return FIXTURE_ROOT / slug


def _repo_dir(slug: str) -> Path:
    return _project_dir(slug) / "repo"


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _ensure_fixture_dirs() -> None:
    for slug, spec in PROJECTS.items():
        repo_dir = _repo_dir(slug)
        repo_dir.mkdir(parents=True, exist_ok=True)
        (_project_dir(slug) / spec["relative_path"]).parent.mkdir(parents=True, exist_ok=True)
        (_project_dir(slug) / spec["relative_path"]).write_text(
            f"# synthetic fixture for {slug}\n{spec['entry_text']}\n{spec['secret_text']}\n",
            encoding="utf-8",
        )
        (repo_dir / spec["relative_path"]).parent.mkdir(parents=True, exist_ok=True)
        (repo_dir / spec["relative_path"]).write_text(
            f"# repo fixture for {slug}\n{spec['entry_text']}\n{spec['secret_text']}\n",
            encoding="utf-8",
        )


def _reset_postgres() -> None:
    with pg_connection() as conn:
        conn.execute(
            "delete from gpc_projects where slug = any(%s)",
            ([ALPHA_SLUG, BETA_SLUG],),
        )


def _reset_qdrant(client: QdrantClient) -> None:
    for slug in (ALPHA_SLUG, BETA_SLUG):
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="project_slug", match=MatchValue(value=slug))]
            ),
        )


def _reset_neo4j() -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            for slug in (ALPHA_SLUG, BETA_SLUG):
                session.run(
                    """
                    MATCH (n)
                    WHERE (n:GraphifyProject AND n.slug = $slug)
                       OR (n:GraphifyRepo AND n.project_slug = $slug)
                       OR (n:GraphifyNode AND n.project_slug = $slug)
                    DETACH DELETE n
                    """,
                    slug=slug,
                )


def _reset_fixture_dirs() -> None:
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)


def _seed_registry_and_storage(client: QdrantClient) -> None:
    batches: dict[str, Any] = {}
    project_rows: dict[str, dict[str, Any]] = {}
    repo_rows: dict[str, dict[str, Any]] = {}

    for slug, spec in PROJECTS.items():
        project = register_project(_project_dir(slug), slug=slug, name=spec["name"])
        repo = register_repo(project["slug"], _repo_dir(slug), slug=spec["repo_slug"], name=spec["repo_slug"])
        project_rows[slug] = project
        repo_rows[slug] = repo
        content = f"{spec['entry_text']}\n\n{spec['secret_text']}"
        batches[slug] = {
            "content": content,
            "title": f"{spec['name']} secret note",
            "file_hash": _content_hash(content),
            "chunk_hash": _content_hash(f"chunk::{content}"),
            "embedding": embed_texts([content]),
        }

    with pg_connection() as conn:
        for slug, spec in PROJECTS.items():
            project = project_rows[slug]
            repo = repo_rows[slug]
            batch = batches[slug]
            file_id = _uid("file", slug)
            chunk_id = _uid("chunk", slug)
            absolute_path = str((_repo_dir(slug) / spec["relative_path"]).resolve())
            conn.execute(
                """
                insert into gpc_files (
                    id, project_id, repo_id, relative_path, absolute_path,
                    language, file_type, size_bytes, content_hash, indexed_at, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                """,
                (
                    file_id,
                    project["id"],
                    repo["id"],
                    spec["relative_path"],
                    absolute_path,
                    "python",
                    "code",
                    len(batch["content"].encode("utf-8")),
                    batch["file_hash"],
                    Jsonb({"repo_slug": spec["repo_slug"]}),
                ),
            )
            conn.execute(
                """
                insert into gpc_chunks (
                    id, project_id, repo_id, file_id, source_type, chunk_type,
                    chunk_index, title, content, content_hash, token_count,
                    qdrant_collection, qdrant_point_id, embedding_model, metadata
                )
                values (%s, %s, %s, %s, 'project_file', %s, 0, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    chunk_id,
                    project["id"],
                    repo["id"],
                    file_id,
                    "paragraph",
                    batch["title"],
                    batch["content"],
                    batch["chunk_hash"],
                    len(batch["content"].split()),
                    COLLECTION_NAME,
                    chunk_id,
                    batch["embedding"].model,
                    Jsonb(
                        {
                            "relative_path": spec["relative_path"],
                            "language": "python",
                            "file_type": "code",
                            "repo_slug": spec["repo_slug"],
                        }
                    ),
                ),
            )

    points: list[PointStruct] = []
    for slug, spec in PROJECTS.items():
        project = project_rows[slug]
        repo = repo_rows[slug]
        batch = batches[slug]
        chunk_id = _uid("chunk", slug)
        file_id = _uid("file", slug)
        vector = batch["embedding"].vectors[0]
        points.append(
            PointStruct(
                id=chunk_id,
                vector=vector,
                payload={
                    "project_id": str(project["id"]),
                    "project_slug": project["slug"],
                    "project_name": project["name"],
                    "root_path": project["root_path"],
                    "repo_id": str(repo["id"]),
                    "repo_slug": repo["slug"],
                    "file_id": file_id,
                    "chunk_id": chunk_id,
                    "relative_path": spec["relative_path"],
                    "language": "python",
                    "file_type": "code",
                    "source_type": "project_file",
                    "chunk_type": "paragraph",
                    "chunk_index": 0,
                    "content_hash": batch["chunk_hash"],
                    "file_hash": batch["file_hash"],
                    "embedding_provider": batch["embedding"].provider,
                    "embedding_model": batch["embedding"].model,
                    "embedding_dimensions": len(vector),
                    "title": batch["title"],
                },
            )
        )
    client.upsert(collection_name=COLLECTION_NAME, points=points)

    with neo4j_driver() as driver:
        with driver.session() as session:
            for slug, spec in PROJECTS.items():
                repo_slug = spec["repo_slug"]
                repo_id = f"{slug}:{repo_slug}"
                entry_id = _graph_id(slug, repo_slug, spec["source_id_entry"])
                secret_id = _graph_id(slug, repo_slug, spec["source_id_secret"])
                session.run(
                    """
                    MERGE (p:GraphifyProject {slug: $slug})
                    SET p.source = 'gpc', p.updated_at = datetime()
                    MERGE (r:GraphifyRepo {id: $repo_id})
                    SET r.slug = $repo_slug, r.project_slug = $slug,
                        r.root_path = $root_path, r.source = 'graphify',
                        r.updated_at = datetime()
                    MERGE (p)-[:HAS_REPO]->(r)
                    MERGE (entry:GraphifyNode {id: $entry_id})
                    SET entry.project_slug = $slug,
                        entry.repo_slug = $repo_slug,
                        entry.repo_id = $repo_id,
                        entry.source_id = $entry_source_id,
                        entry.label = $entry_label,
                        entry.norm_label = $entry_label,
                        entry.file_type = 'code',
                        entry.source_file = $relative_path,
                        entry.community = 0,
                        entry.source = 'graphify'
                    MERGE (secret:GraphifyNode {id: $secret_id})
                    SET secret.project_slug = $slug,
                        secret.repo_slug = $repo_slug,
                        secret.repo_id = $repo_id,
                        secret.source_id = $secret_source_id,
                        secret.label = $secret_label,
                        secret.norm_label = $secret_label,
                        secret.file_type = 'code',
                        secret.source_file = $relative_path,
                        secret.community = 0,
                        secret.source = 'graphify'
                    MERGE (r)-[:HAS_GRAPH_NODE]->(entry)
                    MERGE (r)-[:HAS_GRAPH_NODE]->(secret)
                    MERGE (entry)-[rel:GRAPHIFY_RELATION {id: $relation_id}]->(secret)
                    SET rel.relation = 'calls', rel.project_slug = $slug
                    """,
                    slug=slug,
                    repo_id=repo_id,
                    repo_slug=repo_slug,
                    root_path=str(_repo_dir(slug).resolve()),
                    entry_id=entry_id,
                    secret_id=secret_id,
                    entry_source_id=spec["source_id_entry"],
                    secret_source_id=spec["source_id_secret"],
                    entry_label=spec["entry_label"],
                    secret_label=spec["secret_label"],
                    relative_path=spec["relative_path"],
                    relation_id=_uid("graph-relation", slug),
                )


@contextmanager
def _isolated():
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    _reset_fixture_dirs()
    _reset_postgres()
    _reset_qdrant(client)
    _reset_neo4j()
    _ensure_fixture_dirs()
    try:
        _seed_registry_and_storage(client)
        yield
    finally:
        _reset_postgres()
        _reset_qdrant(client)
        _reset_neo4j()
        _reset_fixture_dirs()


def _as_text(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _assert_absent(payload: Any, forbidden: str) -> None:
    text = _as_text(payload)
    assert forbidden not in text, f"unexpected {forbidden!r} in {text}"


def _assert_search_and_context_isolation() -> None:
    alpha_project, alpha_results = search_project_context(
        "ALPHA_SECRET_TOKEN isolation alpha",
        project=ALPHA_SLUG,
        limit=3,
    )
    assert alpha_project["slug"] == ALPHA_SLUG, alpha_project
    assert alpha_results, "alpha search returned no results"
    assert any(PROJECTS[ALPHA_SLUG]["secret"] in result.content for result in alpha_results), alpha_results
    assert all(PROJECTS[BETA_SLUG]["secret"] not in result.content for result in alpha_results), alpha_results
    assert {result.repo_slug for result in alpha_results} == {PROJECTS[ALPHA_SLUG]["repo_slug"]}, alpha_results

    beta_project, beta_results = search_project_context(
        "BETA_SECRET_TOKEN isolation beta",
        project=BETA_SLUG,
        limit=3,
    )
    assert beta_project["slug"] == BETA_SLUG, beta_project
    assert beta_results, "beta search returned no results"
    assert any(PROJECTS[BETA_SLUG]["secret"] in result.content for result in beta_results), beta_results
    assert all(PROJECTS[ALPHA_SLUG]["secret"] not in result.content for result in beta_results), beta_results
    assert {result.repo_slug for result in beta_results} == {PROJECTS[BETA_SLUG]["repo_slug"]}, beta_results

    _, _, alpha_context = compose_project_context(
        "ALPHA_SECRET_TOKEN isolation alpha",
        project=ALPHA_SLUG,
        max_chunks=3,
        max_chars=4_000,
        include_graph=True,
        graph_min_confidence="EXTRACTED",
    )
    assert PROJECTS[ALPHA_SLUG]["secret"] in alpha_context, alpha_context
    assert PROJECTS[BETA_SLUG]["secret"] not in alpha_context, alpha_context
    assert BETA_SLUG not in alpha_context, alpha_context

    _, _, beta_context = compose_project_context(
        "BETA_SECRET_TOKEN isolation beta",
        project=BETA_SLUG,
        max_chunks=3,
        max_chars=4_000,
        include_graph=True,
        graph_min_confidence="EXTRACTED",
    )
    assert PROJECTS[BETA_SLUG]["secret"] in beta_context, beta_context
    assert PROJECTS[ALPHA_SLUG]["secret"] not in beta_context, beta_context
    assert ALPHA_SLUG not in beta_context, beta_context

    alpha_pack = build_context_pack(
        "ALPHA_SECRET_TOKEN isolation alpha",
        project=ALPHA_SLUG,
        max_chunks=3,
        max_chars=6_000,
        include_graph=True,
    )
    assert alpha_pack.project_slug == ALPHA_SLUG, alpha_pack
    assert PROJECTS[ALPHA_SLUG]["secret"] in alpha_pack.markdown, alpha_pack.markdown
    assert PROJECTS[BETA_SLUG]["secret"] not in alpha_pack.markdown, alpha_pack.markdown

    beta_pack = build_context_pack(
        "BETA_SECRET_TOKEN isolation beta",
        project=BETA_SLUG,
        max_chunks=3,
        max_chars=6_000,
        include_graph=True,
    )
    assert beta_pack.project_slug == BETA_SLUG, beta_pack
    assert PROJECTS[BETA_SLUG]["secret"] in beta_pack.markdown, beta_pack.markdown
    assert PROJECTS[ALPHA_SLUG]["secret"] not in beta_pack.markdown, beta_pack.markdown


def _assert_graph_isolation(slug: str, other_slug: str) -> None:
    spec = PROJECTS[slug]
    other = PROJECTS[other_slug]
    entry_id = _graph_id(slug, spec["repo_slug"], spec["source_id_entry"])
    secret_id = _graph_id(slug, spec["repo_slug"], spec["source_id_secret"])
    cross_id = _graph_id(other_slug, other["repo_slug"], other["source_id_secret"])

    neighbors = graph_neighbors(
        slug,
        entry_id,
        depth=2,
        min_confidence="EXTRACTED",
    )
    assert neighbors["start"]["id"] == entry_id, neighbors
    assert [node["id"] for node in neighbors["neighbors"]] == [secret_id], neighbors
    _assert_absent(neighbors, other_slug)
    _assert_absent(neighbors, other["secret"])

    summary = graph_summary(slug)
    assert summary["found"] is True, summary
    assert [repo["repo"] for repo in summary["repos"]] == [spec["repo_slug"]], summary
    summary_labels = {node["label"] for node in summary["god_nodes"]} | {
        node["label"] for node in summary["utility_hubs"]
    }
    assert summary_labels == {spec["entry_label"], spec["secret_label"]}, summary
    assert summary["cross_repo_bridges"] == [], summary
    _assert_absent(summary, other_slug)
    _assert_absent(summary, other["secret"])

    path = graph_path(
        slug,
        entry_id,
        secret_id,
        min_confidence="EXTRACTED",
    )
    assert path["path"] is not None, path
    assert path["path"]["length"] == 1, path
    assert path["path"]["hops"][0]["relation"] == "GRAPHIFY_RELATION", path
    _assert_absent(path, other_slug)
    _assert_absent(path, other["secret"])

    blocked = graph_path(
        slug,
        entry_id,
        cross_id,
        min_confidence="EXTRACTED",
    )
    assert blocked["path"] is None, blocked
    assert blocked["reason"] == "node_not_found", blocked


def main() -> None:
    with _isolated():
        _assert_search_and_context_isolation()
        _assert_graph_isolation(ALPHA_SLUG, BETA_SLUG)
        _assert_graph_isolation(BETA_SLUG, ALPHA_SLUG)

    print("cross_project_isolation_smoke_test=passed")


if __name__ == "__main__":
    main()
