"""Cross-repository bridging for the Graphify Neo4j projection.

Each repository pushes its own isolated Graphify subgraph. This module stitches
those subgraphs together at the Neo4j layer by creating ``CROSS_REPO_BRIDGE``
relationships between ``GraphifyNode`` nodes that belong to the same
``GraphifyProject`` but different ``GraphifyRepo``.

Design goals:
    - scope strictly by ``project_slug`` — no cross-project bridging.
    - tiered confidence so downstream clients can filter noise.
    - idempotent via ``MERGE`` on ``(a, b, rule)`` triples.
    - never emit ``EXTRACTED`` — these are heuristic inferences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from gpc.config import POSTGRES_DSN
from gpc.graph import neo4j_driver


RULE_SAME_SOURCE_FILE = "same_source_file"
RULE_SAME_CODE_SYMBOL = "same_code_symbol"
RULE_SAME_GENERIC_SYMBOL = "same_generic_symbol"
RULE_CONTENT_HASH = "content_hash"

DEFAULT_RULES: tuple[str, ...] = (
    RULE_CONTENT_HASH,
    RULE_SAME_SOURCE_FILE,
    RULE_SAME_CODE_SYMBOL,
)
ALL_RULES: tuple[str, ...] = (
    RULE_CONTENT_HASH,
    RULE_SAME_SOURCE_FILE,
    RULE_SAME_CODE_SYMBOL,
    RULE_SAME_GENERIC_SYMBOL,
)


GENERIC_LABEL_STEMS = frozenset(
    {
        "main", "run", "start", "stop", "init", "setup", "teardown",
        "fetch", "get", "set", "post", "put", "delete", "handle", "handler",
        "execute", "dispatch", "process", "apply", "call", "invoke",
        "test", "tests", "spec", "mock", "stub",
        "parse", "format", "serialize", "deserialize", "render",
        "open", "close", "read", "write", "load", "save",
        "index", "config", "app", "server", "client",
        "utils", "util", "helpers", "helper", "types", "constants",
        "readme", "license", "contributing", "changelog", "dockerfile",
        "package", "tsconfig", "eslint", "prettier", "vitest", "jest",
        "webpack", "rollup", "vite", "babel", "wrangler", "gitignore",
        "makefile",
    }
)


def is_generic_label(label: str | None) -> bool:
    """Return ``True`` when a label is too common to bridge with high confidence.

    Also used by ``graph_query.graph_summary`` to split god nodes between
    "central" (distinctive) and "utility_hub" (generic bootstrap / dispatch).
    """

    if not label:
        return True
    base = label.strip().lower()
    if base.endswith("()"):
        base = base[:-2]
    if len(base) < 4:
        return True
    stems = [part for part in re.split(r"[.\-_/\s]+", base) if part]
    if not stems:
        return True
    return any(stem in GENERIC_LABEL_STEMS for stem in stems)


# Backwards-compatible alias for the original private name.
_is_generic_label = is_generic_label


@dataclass
class BridgeStats:
    project_slug: str
    repos: int
    pairs_content_hash: int = 0
    pairs_same_source_file: int = 0
    pairs_same_code_symbol: int = 0
    pairs_same_generic_symbol: int = 0
    edges_written: int = 0
    edges_deleted: int = 0
    rules_applied: tuple[str, ...] = field(default_factory=tuple)


def ensure_bridge_indexes() -> None:
    """Create Neo4j indexes that speed up bridging queries."""

    statements = [
        "CREATE INDEX graphify_node_project_slug IF NOT EXISTS "
        "FOR (n:GraphifyNode) ON (n.project_slug)",
        "CREATE INDEX graphify_node_norm_label IF NOT EXISTS "
        "FOR (n:GraphifyNode) ON (n.norm_label)",
        "CREATE INDEX graphify_node_source_file IF NOT EXISTS "
        "FOR (n:GraphifyNode) ON (n.source_file)",
    ]
    with neo4j_driver() as driver:
        with driver.session() as session:
            for stmt in statements:
                session.run(stmt)


def list_graphify_projects() -> list[str]:
    with neo4j_driver() as driver:
        with driver.session() as session:
            rows = session.run(
                "MATCH (p:GraphifyProject) RETURN p.slug AS slug ORDER BY slug"
            ).data()
    return [row["slug"] for row in rows if row.get("slug")]


def clear_bridges(project_slug: str) -> int:
    """Delete every ``CROSS_REPO_BRIDGE`` edge for the given project."""

    with neo4j_driver() as driver:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (a:GraphifyNode {project_slug: $p})
                      -[r:CROSS_REPO_BRIDGE]->
                      (b:GraphifyNode {project_slug: $p})
                WITH r
                DELETE r
                RETURN count(*) AS c
                """,
                p=project_slug,
            ).single()
    return int(result["c"]) if result else 0


def _repo_count(session, project_slug: str) -> int:
    record = session.run(
        "MATCH (:GraphifyProject {slug: $p})-[:HAS_REPO]->(r:GraphifyRepo) RETURN count(r) AS c",
        p=project_slug,
    ).single()
    return int(record["c"]) if record else 0


def _collect_content_hash_pairs(project_slug: str) -> list[dict]:
    """Pair GraphifyNodes whose underlying file bytes are identical.

    Joins the Neo4j GraphifyNode identity (``project_slug``, ``repo_slug``,
    ``source_file``) with ``gpc_files.content_hash`` in Postgres. Strong
    signal: if two repos have the same SHA, the file was literally copied.
    """

    # 1. Pull hashes from Postgres, scoped to the project.
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        file_rows = conn.execute(
            """
            select r.slug as repo_slug,
                   f.relative_path,
                   f.content_hash
            from gpc_files f
            join gpc_repos r on r.id = f.repo_id
            join gpc_projects p on p.id = f.project_id
            where p.slug = %s
              and f.content_hash is not null
            """,
            (project_slug,),
        ).fetchall()

    # Group by content_hash — only hashes that appear in ≥2 repos are
    # interesting (same file duplicated across services).
    by_hash: dict[str, list[tuple[str, str]]] = {}
    for row in file_rows:
        key = row["content_hash"]
        by_hash.setdefault(key, []).append((row["repo_slug"], row["relative_path"]))

    # Keep only hashes where at least two distinct repos participate.
    targets: list[dict] = []
    for digest, locations in by_hash.items():
        repos = {repo for repo, _ in locations}
        if len(repos) < 2:
            continue
        targets.append(
            {
                "hash": digest,
                "locations": locations,
            }
        )
    if not targets:
        return []

    # 2. Match GraphifyNodes in Neo4j for those (repo_slug, source_file)
    # tuples, then emit one row per cross-repo pair of GraphifyNode ids.
    locations_flat: list[dict[str, str]] = []
    for target in targets:
        for repo_slug, rel in target["locations"]:
            locations_flat.append({"repo": repo_slug, "path": rel, "hash": target["hash"]})

    with neo4j_driver() as driver:
        with driver.session() as session:
            rows = session.run(
                """
                UNWIND $locations AS loc
                MATCH (n:GraphifyNode {project_slug: $slug})
                WHERE n.repo_slug = loc.repo AND n.source_file = loc.path
                RETURN loc.hash AS hash, loc.repo AS repo, loc.path AS path,
                       n.id AS node_id, n.label AS label
                """,
                slug=project_slug,
                locations=locations_flat,
            ).data()

    # Group matched nodes by hash.
    nodes_by_hash: dict[str, list[dict]] = {}
    for row in rows:
        nodes_by_hash.setdefault(row["hash"], []).append(row)

    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for digest, candidates in nodes_by_hash.items():
        for i, a in enumerate(candidates):
            for b in candidates[i + 1 :]:
                if a["repo"] == b["repo"]:
                    continue
                # Canonical ordering so (a, b) and (b, a) count as one pair.
                pair = (a["node_id"], b["node_id"])
                if pair[0] > pair[1]:
                    pair = (pair[1], pair[0])
                    a_id, b_id = pair
                else:
                    a_id, b_id = pair
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append(
                    {
                        "a_id": a_id,
                        "b_id": b_id,
                        "evidence": f"sha:{digest[:12]}… ({a['path']})",
                    }
                )
    return pairs


def _collect_same_source_file(session, project_slug: str) -> list[dict]:
    rows = session.run(
        """
        MATCH (a:GraphifyNode {project_slug: $p}),
              (b:GraphifyNode {project_slug: $p})
        WHERE a.id < b.id
          AND a.repo_slug <> b.repo_slug
          AND a.source_file IS NOT NULL
          AND a.source_file <> ""
          AND a.source_file = b.source_file
        RETURN a.id AS a_id, b.id AS b_id,
               a.label AS label, a.source_file AS evidence
        """,
        p=project_slug,
    ).data()
    filtered: list[dict] = []
    for row in rows:
        label = row.get("label")
        if _is_generic_label(label):
            continue
        filtered.append(row)
    return filtered


def _collect_same_label(session, project_slug: str) -> list[dict]:
    return session.run(
        """
        MATCH (a:GraphifyNode {project_slug: $p}),
              (b:GraphifyNode {project_slug: $p})
        WHERE a.id < b.id
          AND a.repo_slug <> b.repo_slug
          AND coalesce(a.file_type, '') = coalesce(b.file_type, '')
          AND a.norm_label IS NOT NULL
          AND a.norm_label <> ""
          AND a.norm_label = b.norm_label
        RETURN a.id AS a_id, b.id AS b_id,
               a.label AS label, a.file_type AS kind,
               a.source_file AS a_src, b.source_file AS b_src
        """,
        p=project_slug,
    ).data()


def _write_bridges(
    session,
    project_slug: str,
    rows: Sequence[dict],
    *,
    rule: str,
    confidence: str,
    score: float,
) -> None:
    if not rows:
        return
    session.run(
        """
        UNWIND $rows AS row
        MATCH (a:GraphifyNode {id: row.a_id})
        MATCH (b:GraphifyNode {id: row.b_id})
        MERGE (a)-[r:CROSS_REPO_BRIDGE {rule: $rule}]->(b)
        SET r.confidence = $confidence,
            r.confidence_score = $score,
            r.project_slug = $project_slug,
            r.source = 'gpc',
            r.evidence = row.evidence,
            r.updated_at = datetime()
        """,
        rows=list(rows),
        rule=rule,
        confidence=confidence,
        score=score,
        project_slug=project_slug,
    )


def build_bridges(
    project_slug: str,
    *,
    rules: Iterable[str] = DEFAULT_RULES,
    clear_existing: bool = False,
) -> BridgeStats:
    """Create cross-repo bridge edges for all ``GraphifyNode`` pairs in a project.

    Args:
        project_slug: restrict to a single ``GraphifyProject.slug``. Required.
        rules: subset of ``ALL_RULES``. Default is
            ``(RULE_SAME_SOURCE_FILE, RULE_SAME_CODE_SYMBOL)`` — no ``AMBIGUOUS``
            noise.
        clear_existing: delete existing bridges for this project before writing.
    """

    if not project_slug:
        raise ValueError("project_slug is required — bridging is always scoped by project")

    active_rules = tuple(r for r in rules if r in ALL_RULES)
    if not active_rules:
        raise ValueError(f"No valid rules supplied. Choose from {ALL_RULES!r}")

    ensure_bridge_indexes()

    stats = BridgeStats(project_slug=project_slug, repos=0, rules_applied=active_rules)

    with neo4j_driver() as driver:
        with driver.session() as session:
            stats.repos = _repo_count(session, project_slug)
            if stats.repos < 2:
                return stats

            if clear_existing:
                stats.edges_deleted = clear_bridges(project_slug)

            # Rule 1 (strongest): content_hash. Runs first so weaker rules
            # can skip pairs already bridged by a SHA match.
            content_hash_pairs: list[dict] = []
            if RULE_CONTENT_HASH in active_rules:
                content_hash_pairs = _collect_content_hash_pairs(project_slug)
            stats.pairs_content_hash = len(content_hash_pairs)
            already_hash_bridged = {(p["a_id"], p["b_id"]) for p in content_hash_pairs}

            same_file_pairs: list[dict] = []
            if RULE_SAME_SOURCE_FILE in active_rules:
                same_file_pairs = [
                    row for row in _collect_same_source_file(session, project_slug)
                    if (row["a_id"], row["b_id"]) not in already_hash_bridged
                ]
            stats.pairs_same_source_file = len(same_file_pairs)

            already_file_bridged = {(row["a_id"], row["b_id"]) for row in same_file_pairs}
            already_file_bridged |= already_hash_bridged

            symbol_distinct: list[dict] = []
            symbol_generic: list[dict] = []
            if (
                RULE_SAME_CODE_SYMBOL in active_rules
                or RULE_SAME_GENERIC_SYMBOL in active_rules
            ):
                for row in _collect_same_label(session, project_slug):
                    if (row["a_id"], row["b_id"]) in already_file_bridged:
                        continue
                    label = row.get("label")
                    kind = row.get("kind") or ""
                    bucket = symbol_distinct if (
                        kind == "code" and not _is_generic_label(label)
                    ) else symbol_generic
                    bucket.append(
                        {
                            "a_id": row["a_id"],
                            "b_id": row["b_id"],
                            "evidence": label,
                        }
                    )
            stats.pairs_same_code_symbol = len(symbol_distinct)
            stats.pairs_same_generic_symbol = len(symbol_generic)

            if RULE_CONTENT_HASH in active_rules and content_hash_pairs:
                _write_bridges(
                    session,
                    project_slug,
                    content_hash_pairs,
                    rule=RULE_CONTENT_HASH,
                    confidence="INFERRED",
                    score=0.95,
                )
                stats.edges_written += len(content_hash_pairs)

            file_rows = [
                {"a_id": r["a_id"], "b_id": r["b_id"], "evidence": r["evidence"]}
                for r in same_file_pairs
            ]
            if RULE_SAME_SOURCE_FILE in active_rules:
                _write_bridges(
                    session,
                    project_slug,
                    file_rows,
                    rule=RULE_SAME_SOURCE_FILE,
                    confidence="INFERRED",
                    score=0.9,
                )
                stats.edges_written += len(file_rows)

            if RULE_SAME_CODE_SYMBOL in active_rules:
                _write_bridges(
                    session,
                    project_slug,
                    symbol_distinct,
                    rule=RULE_SAME_CODE_SYMBOL,
                    confidence="INFERRED",
                    score=0.75,
                )
                stats.edges_written += len(symbol_distinct)

            if RULE_SAME_GENERIC_SYMBOL in active_rules:
                _write_bridges(
                    session,
                    project_slug,
                    symbol_generic,
                    rule=RULE_SAME_GENERIC_SYMBOL,
                    confidence="AMBIGUOUS",
                    score=0.3,
                )
                stats.edges_written += len(symbol_generic)

    # Longitudinal snapshot — cheap at the end of bridging.
    try:
        from gpc.self_metrics import collect_metrics

        collect_metrics(project_slug=project_slug, source="graph-bridge")
    except Exception:  # noqa: BLE001 — never block bridging
        pass

    return stats


def build_bridges_all_projects(
    *,
    rules: Iterable[str] = DEFAULT_RULES,
    clear_existing: bool = False,
) -> list[BridgeStats]:
    """Run :func:`build_bridges` for every ``GraphifyProject`` present in Neo4j."""

    results: list[BridgeStats] = []
    for slug in list_graphify_projects():
        results.append(
            build_bridges(slug, rules=rules, clear_existing=clear_existing)
        )
    return results
