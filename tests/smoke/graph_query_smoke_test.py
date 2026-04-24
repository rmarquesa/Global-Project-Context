"""Smoke test for graph_neighbors / graph_summary / graph_path.

Builds a synthetic project with two repos, EXTRACTED GRAPHIFY_RELATION edges,
and one CROSS_REPO_BRIDGE, then exercises each tool:

* neighbors respects min_confidence (bridge hidden at EXTRACTED, visible at INFERRED)
* neighbors respects relations filter
* summary returns god nodes, repo breakdown and bridge counts
* path returns the shortest path with typed hops
* every query is scoped by project_slug — neighbour project is never touched

Always cleans up seeded data, even on failure.
"""

from __future__ import annotations

from contextlib import contextmanager

from gpc.graph import neo4j_driver
from gpc.graph_query import graph_neighbors, graph_path, graph_summary


TEST_PROJECT_SLUG = "gpc_smoke_graph_query"
NEIGHBOUR_SLUG = "gpc_smoke_graph_query_other"


SEED_NODES = [
    # repo alpha
    {"repo": "alpha", "id": "alpha_main", "label": "main()", "source_file": "src/main.js", "file_type": "code"},
    {"repo": "alpha", "id": "alpha_auth", "label": "authenticate()", "source_file": "src/auth.js", "file_type": "code"},
    {"repo": "alpha", "id": "alpha_db", "label": "getDatabase()", "source_file": "src/db.js", "file_type": "code"},
    # repo beta
    {"repo": "beta", "id": "beta_main", "label": "main()", "source_file": "src/main.js", "file_type": "code"},
    {"repo": "beta", "id": "beta_auth", "label": "authenticate()", "source_file": "src/auth.js", "file_type": "code"},
]


SEED_GRAPHIFY_EDGES = [
    # alpha: main -> auth -> db
    {"src": "alpha_main", "tgt": "alpha_auth", "relation": "calls"},
    {"src": "alpha_auth", "tgt": "alpha_db", "relation": "calls"},
    # beta: main -> auth
    {"src": "beta_main", "tgt": "beta_auth", "relation": "calls"},
]


SEED_BRIDGE_EDGES = [
    # cross-repo bridge by same_source_file authenticate.js
    {
        "src": "alpha_auth",
        "tgt": "beta_auth",
        "rule": "same_source_file",
        "confidence": "INFERRED",
        "confidence_score": 0.9,
    },
]


def _qid(slug: str, repo: str, sid: str) -> str:
    return f"{slug}:{repo}:{sid}"


def _seed(slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                "MERGE (p:GraphifyProject {slug: $slug}) SET p.source='gpc', p.updated_at=datetime()",
                slug=slug,
            )
            for repo in {n["repo"] for n in SEED_NODES}:
                session.run(
                    """
                    MATCH (p:GraphifyProject {slug: $slug})
                    MERGE (r:GraphifyRepo {id: $rid})
                    SET r.slug=$repo, r.project_slug=$slug, r.source='graphify',
                        r.updated_at=datetime(), r.root_path='/tmp/'+$repo
                    MERGE (p)-[:HAS_REPO]->(r)
                    """,
                    slug=slug, rid=f"{slug}:{repo}", repo=repo,
                )
            for n in SEED_NODES:
                session.run(
                    """
                    MATCH (r:GraphifyRepo {id: $rid})
                    MERGE (node:GraphifyNode {id: $id})
                    SET node.project_slug=$slug, node.repo_slug=$repo,
                        node.repo_id=$rid, node.source_id=$sid,
                        node.label=$label, node.norm_label=$label,
                        node.file_type=$ftype, node.source_file=$src,
                        node.community=0, node.source='graphify'
                    MERGE (r)-[:HAS_GRAPH_NODE]->(node)
                    """,
                    rid=f"{slug}:{n['repo']}",
                    id=_qid(slug, n["repo"], n["id"]),
                    slug=slug, repo=n["repo"], sid=n["id"],
                    label=n["label"], ftype=n["file_type"], src=n["source_file"],
                )
            for i, e in enumerate(SEED_GRAPHIFY_EDGES):
                # Map src/tgt id to (slug, repo, sid) — assume same repo for both
                # by looking up the seed node.
                src_node = next(n for n in SEED_NODES if n["id"] == e["src"])
                tgt_node = next(n for n in SEED_NODES if n["id"] == e["tgt"])
                session.run(
                    """
                    MATCH (a:GraphifyNode {id: $a_id}), (b:GraphifyNode {id: $b_id})
                    MERGE (a)-[r:GRAPHIFY_RELATION {id: $eid}]->(b)
                    SET r.relation = $rel, r.project_slug = $slug
                    """,
                    a_id=_qid(slug, src_node["repo"], e["src"]),
                    b_id=_qid(slug, tgt_node["repo"], e["tgt"]),
                    eid=f"{slug}:{i}:{e['relation']}",
                    rel=e["relation"],
                    slug=slug,
                )
            for e in SEED_BRIDGE_EDGES:
                src_node = next(n for n in SEED_NODES if n["id"] == e["src"])
                tgt_node = next(n for n in SEED_NODES if n["id"] == e["tgt"])
                session.run(
                    """
                    MATCH (a:GraphifyNode {id: $a_id}), (b:GraphifyNode {id: $b_id})
                    MERGE (a)-[r:CROSS_REPO_BRIDGE {rule: $rule}]->(b)
                    SET r.confidence = $conf, r.confidence_score = $score,
                        r.project_slug = $slug, r.source = 'gpc',
                        r.updated_at = datetime()
                    """,
                    a_id=_qid(slug, src_node["repo"], e["src"]),
                    b_id=_qid(slug, tgt_node["repo"], e["tgt"]),
                    rule=e["rule"], conf=e["confidence"], score=e["confidence_score"],
                    slug=slug,
                )


def _cleanup(slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
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


@contextmanager
def _isolated():
    _cleanup(TEST_PROJECT_SLUG)
    _cleanup(NEIGHBOUR_SLUG)
    try:
        _seed(TEST_PROJECT_SLUG)
        _seed(NEIGHBOUR_SLUG)
        yield
    finally:
        _cleanup(TEST_PROJECT_SLUG)
        _cleanup(NEIGHBOUR_SLUG)


def main() -> None:
    with _isolated():
        # 1. Neighbors — EXTRACTED only (bridge INFERRED must be hidden).
        # Use the fully qualified id to make the pick deterministic.
        alpha_auth_id = _qid(TEST_PROJECT_SLUG, "alpha", "alpha_auth")
        result = graph_neighbors(
            TEST_PROJECT_SLUG,
            alpha_auth_id,
            depth=1,
            min_confidence="EXTRACTED",
        )
        labels = {n["label"] for n in result["neighbors"]}
        assert result["start"]["id"] == alpha_auth_id, result
        assert labels == {"main()", "getDatabase()"}, f"EXTRACTED neighbors wrong: {labels}"

        # 2. Neighbors — INFERRED includes the cross-repo bridge.
        result = graph_neighbors(
            TEST_PROJECT_SLUG,
            alpha_auth_id,
            depth=1,
            min_confidence="INFERRED",
        )
        edges_relations = {
            e["relation"] for n in result["neighbors"] for e in n["edges"]
        }
        assert "CROSS_REPO_BRIDGE" in edges_relations, edges_relations

        # 3. Neighbors — relations filter restricts to GRAPHIFY_RELATION only.
        result = graph_neighbors(
            TEST_PROJECT_SLUG,
            alpha_auth_id,
            depth=1,
            min_confidence="INFERRED",
            relations=["GRAPHIFY_RELATION"],
        )
        assert all(
            e["relation"] == "GRAPHIFY_RELATION"
            for n in result["neighbors"]
            for e in n["edges"]
        ), result

        # 4. Summary — returns repos, gods, and bridges.
        summary = graph_summary(TEST_PROJECT_SLUG)
        assert summary["found"] is True, summary
        repo_slugs = {r["repo"] for r in summary["repos"]}
        assert repo_slugs == {"alpha", "beta"}, repo_slugs
        god_labels = {g["label"] for g in summary["god_nodes"]}
        assert "authenticate()" in god_labels, god_labels
        bridge_rules = {b["rule"] for b in summary["cross_repo_bridges"]}
        assert "same_source_file" in bridge_rules, bridge_rules

        # 5. Path — from alpha.main() to beta.authenticate() crosses the bridge.
        alpha_main_id = _qid(TEST_PROJECT_SLUG, "alpha", "alpha_main")
        beta_auth_id = _qid(TEST_PROJECT_SLUG, "beta", "beta_auth")
        path = graph_path(
            TEST_PROJECT_SLUG,
            alpha_main_id,
            beta_auth_id,
            min_confidence="INFERRED",
        )
        assert path["path"] is not None, path
        assert path["path"]["length"] == 2, path["path"]
        relations_on_path = [h["relation"] for h in path["path"]["hops"]]
        assert "CROSS_REPO_BRIDGE" in relations_on_path, relations_on_path

        # 6. Path — EXTRACTED-only rejects the same path (bridge is INFERRED).
        strict = graph_path(
            TEST_PROJECT_SLUG,
            alpha_main_id,
            beta_auth_id,
            min_confidence="EXTRACTED",
        )
        assert strict["path"] is None, strict
        assert strict["reason"] in {
            "path_below_min_confidence",
            "no_path_within_max_hops",
        }, strict

        # 7. Scope isolation — neighbour project is not visible.
        result = graph_neighbors(
            TEST_PROJECT_SLUG,
            "authenticate",
            depth=3,
            min_confidence="INFERRED",
        )
        for n in result["neighbors"]:
            assert NEIGHBOUR_SLUG not in n["id"], n

    print("graph_query_smoke_test=passed")


if __name__ == "__main__":
    main()
