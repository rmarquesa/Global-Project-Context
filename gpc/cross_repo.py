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

from gpc.graph import neo4j_driver


RULE_SAME_SOURCE_FILE = "same_source_file"
RULE_SAME_CODE_SYMBOL = "same_code_symbol"
RULE_SAME_GENERIC_SYMBOL = "same_generic_symbol"

DEFAULT_RULES: tuple[str, ...] = (RULE_SAME_SOURCE_FILE, RULE_SAME_CODE_SYMBOL)
ALL_RULES: tuple[str, ...] = (
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


def _is_generic_label(label: str | None) -> bool:
    """Return ``True`` when a label is too common to bridge with high confidence."""

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


@dataclass
class BridgeStats:
    project_slug: str
    repos: int
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

            same_file_pairs: list[dict] = []
            if RULE_SAME_SOURCE_FILE in active_rules:
                same_file_pairs = _collect_same_source_file(session, project_slug)
            stats.pairs_same_source_file = len(same_file_pairs)

            already_file_bridged = {(row["a_id"], row["b_id"]) for row in same_file_pairs}

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
