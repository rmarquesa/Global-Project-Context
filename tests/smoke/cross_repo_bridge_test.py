"""Smoke test for cross-repo bridging in Neo4j.

Creates an isolated synthetic project (``gpc_smoke_bridge``) with two repos and
a handful of nodes designed to exercise each bridging rule, then runs
``build_bridges`` and asserts the expected ``CROSS_REPO_BRIDGE`` edges exist.

Requires a running Neo4j with credentials resolvable through ``gpc.config``.
Always cleans up its own data, whether the assertions pass or fail.
"""

from __future__ import annotations

from contextlib import contextmanager

from gpc.cross_repo import (
    ALL_RULES,
    RULE_SAME_CODE_SYMBOL,
    RULE_SAME_GENERIC_SYMBOL,
    RULE_SAME_SOURCE_FILE,
    build_bridges,
    clear_bridges,
)
from gpc.graph import neo4j_driver


TEST_PROJECT_SLUG = "gpc_smoke_bridge"


SEED_NODES: list[dict] = [
    # Distinctive function, same label in both repos → same_code_symbol
    {
        "repo": "repo_a",
        "source_id": "auth_computeauthsignature",
        "label": "computeAuthSignature()",
        "file_type": "code",
        "source_file": "src/lib/auth.js",
    },
    {
        "repo": "repo_b",
        "source_id": "auth_computeauthsignature",
        "label": "computeAuthSignature()",
        "file_type": "code",
        "source_file": "src/auth.js",
    },
    # Same source_file relative path → same_source_file (stronger than same_code_symbol)
    {
        "repo": "repo_a",
        "source_id": "shared_cryptohelpers",
        "label": "cryptoHelpers()",
        "file_type": "code",
        "source_file": "shared/crypto-helpers.js",
    },
    {
        "repo": "repo_b",
        "source_id": "shared_cryptohelpers",
        "label": "cryptoHelpers()",
        "file_type": "code",
        "source_file": "shared/crypto-helpers.js",
    },
    # Generic label in both repos → same_generic_symbol (opt-in only)
    {
        "repo": "repo_a",
        "source_id": "main",
        "label": "main()",
        "file_type": "code",
        "source_file": "repo_a_main.js",
    },
    {
        "repo": "repo_b",
        "source_id": "main",
        "label": "main()",
        "file_type": "code",
        "source_file": "repo_b_main.js",
    },
    # Same label in only one repo → no bridge expected
    {
        "repo": "repo_a",
        "source_id": "onlyhere",
        "label": "uniqueHandler()",
        "file_type": "code",
        "source_file": "src/unique.js",
    },
]


def _seed() -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                "MERGE (p:GraphifyProject {slug: $slug}) SET p.source='gpc', p.updated_at=datetime()",
                slug=TEST_PROJECT_SLUG,
            )
            for repo in {n["repo"] for n in SEED_NODES}:
                session.run(
                    """
                    MATCH (p:GraphifyProject {slug: $slug})
                    MERGE (r:GraphifyRepo {id: $repo_id})
                    SET r.slug = $repo, r.project_slug = $slug,
                        r.source = 'graphify', r.updated_at = datetime()
                    MERGE (p)-[:HAS_REPO]->(r)
                    """,
                    slug=TEST_PROJECT_SLUG,
                    repo_id=f"{TEST_PROJECT_SLUG}:{repo}",
                    repo=repo,
                )
            for node in SEED_NODES:
                global_id = f"{TEST_PROJECT_SLUG}:{node['repo']}:{node['source_id']}"
                session.run(
                    """
                    MATCH (r:GraphifyRepo {id: $repo_id})
                    MERGE (n:GraphifyNode {id: $id})
                    SET n.project_slug = $slug,
                        n.repo_slug = $repo,
                        n.repo_id = $repo_id,
                        n.source_id = $source_id,
                        n.label = $label,
                        n.norm_label = $label,
                        n.file_type = $file_type,
                        n.source_file = $source_file,
                        n.source = 'graphify',
                        n.updated_at = datetime()
                    MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                    """,
                    repo_id=f"{TEST_PROJECT_SLUG}:{node['repo']}",
                    id=global_id,
                    slug=TEST_PROJECT_SLUG,
                    repo=node["repo"],
                    source_id=node["source_id"],
                    label=node["label"],
                    file_type=node["file_type"],
                    source_file=node["source_file"],
                )


def _cleanup() -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})
                OPTIONAL MATCH (p)-[:HAS_REPO]->(r:GraphifyRepo)
                OPTIONAL MATCH (r)-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                DETACH DELETE n, r, p
                """,
                slug=TEST_PROJECT_SLUG,
            )


def _seed_second_project() -> None:
    other = f"{TEST_PROJECT_SLUG}_other"
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                "MERGE (p:GraphifyProject {slug: $slug}) SET p.source='gpc', p.updated_at=datetime()",
                slug=other,
            )
            for repo in ("repo_a", "repo_b"):
                session.run(
                    """
                    MATCH (p:GraphifyProject {slug: $slug})
                    MERGE (r:GraphifyRepo {id: $repo_id})
                    SET r.slug = $repo, r.project_slug = $slug,
                        r.source = 'graphify', r.updated_at = datetime()
                    MERGE (p)-[:HAS_REPO]->(r)
                    """,
                    slug=other,
                    repo_id=f"{other}:{repo}",
                    repo=repo,
                )
            session.run(
                """
                MATCH (r:GraphifyRepo {id: $repo_id})
                MERGE (n:GraphifyNode {id: $id})
                SET n.project_slug = $slug,
                    n.repo_slug = $repo,
                    n.repo_id = $repo_id,
                    n.source_id = 'otherSymbol',
                    n.label = 'otherSymbol()',
                    n.norm_label = 'otherSymbol()',
                    n.file_type = 'code',
                    n.source_file = 'src/other.js',
                    n.source = 'graphify',
                    n.updated_at = datetime()
                MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                """,
                repo_id=f"{other}:repo_a",
                id=f"{other}:repo_a:otherSymbol",
                slug=other,
                repo="repo_a",
            )
            session.run(
                """
                MATCH (r:GraphifyRepo {id: $repo_id})
                MERGE (n:GraphifyNode {id: $id})
                SET n.project_slug = $slug,
                    n.repo_slug = $repo,
                    n.repo_id = $repo_id,
                    n.source_id = 'otherSymbol',
                    n.label = 'otherSymbol()',
                    n.norm_label = 'otherSymbol()',
                    n.file_type = 'code',
                    n.source_file = 'src/other.js',
                    n.source = 'graphify',
                    n.updated_at = datetime()
                MERGE (r)-[:HAS_GRAPH_NODE]->(n)
                """,
                repo_id=f"{other}:repo_b",
                id=f"{other}:repo_b:otherSymbol",
                slug=other,
                repo="repo_b",
            )


def _cleanup_second_project() -> None:
    other = f"{TEST_PROJECT_SLUG}_other"
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})
                OPTIONAL MATCH (p)-[:HAS_REPO]->(r:GraphifyRepo)
                OPTIONAL MATCH (r)-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                DETACH DELETE n, r, p
                """,
                slug=other,
            )


def _count_bridges(rule: str | None = None) -> int:
    query = (
        "MATCH (:GraphifyNode {project_slug: $slug})"
        "-[r:CROSS_REPO_BRIDGE]->"
        "(:GraphifyNode {project_slug: $slug}) "
    )
    if rule:
        query += "WHERE r.rule = $rule "
    query += "RETURN count(r) AS c"
    with neo4j_driver() as driver:
        with driver.session() as session:
            record = session.run(
                query, slug=TEST_PROJECT_SLUG, rule=rule
            ).single()
    return int(record["c"]) if record else 0


@contextmanager
def _isolated_test():
    _cleanup()
    try:
        _seed()
        yield
    finally:
        _cleanup()


def main() -> None:
    with _isolated_test():
        # 1. Default rules — no AMBIGUOUS edges written, even though a generic
        #    pair exists and is reported in the diagnostic counters.
        stats = build_bridges(TEST_PROJECT_SLUG, clear_existing=True)
        assert stats.repos == 2, stats
        assert stats.pairs_same_source_file == 1, stats
        assert stats.pairs_same_code_symbol == 1, stats
        assert stats.pairs_same_generic_symbol == 1, stats
        assert stats.edges_written == 2, stats
        assert _count_bridges(RULE_SAME_SOURCE_FILE) == 1
        assert _count_bridges(RULE_SAME_CODE_SYMBOL) == 1
        assert _count_bridges(RULE_SAME_GENERIC_SYMBOL) == 0, "generic rule must not write without opt-in"

        # 2. Explicit include of generic rule — AMBIGUOUS bridge shows up.
        stats = build_bridges(
            TEST_PROJECT_SLUG,
            rules=ALL_RULES,
            clear_existing=True,
        )
        assert stats.pairs_same_generic_symbol == 1, stats
        assert stats.edges_written == 3, stats
        assert _count_bridges(RULE_SAME_GENERIC_SYMBOL) == 1

        # 3. Idempotency — re-running does not duplicate edges.
        stats = build_bridges(
            TEST_PROJECT_SLUG,
            rules=ALL_RULES,
            clear_existing=False,
        )
        assert _count_bridges() == 3, _count_bridges()

        # 4. clear_bridges wipes the project's bridges.
        deleted = clear_bridges(TEST_PROJECT_SLUG)
        assert deleted == 3, deleted
        assert _count_bridges() == 0

        # 5. Scope isolation — bridging another project does not touch this one.
        _seed_second_project()
        try:
            build_bridges(f"{TEST_PROJECT_SLUG}_other", clear_existing=True)
            assert _count_bridges() == 0, "bridges in test project must not appear from a different project run"
        finally:
            _cleanup_second_project()

    print("cross_repo_bridge_test=passed")


if __name__ == "__main__":
    main()
