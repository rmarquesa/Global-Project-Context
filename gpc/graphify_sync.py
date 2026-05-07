from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from gpc.graph import neo4j_driver


@dataclass(frozen=True)
class GraphifySyncStats:
    project_slug: str
    repo_slug: str
    nodes_written: int
    relations_written: int
    repos_written: int = 1


def sync_graphify_to_neo4j(
    repo_root: str | Path,
    *,
    project_slug: str,
    repo_slug: str,
    clear_repo: bool = True,
) -> GraphifySyncStats:
    """Project a local ``graphify-out/graph.json`` file into Neo4j.

    Graphify writes local artifacts under each repository. GPC graph MCP tools
    read the consolidated Neo4j projection, so this sync bridges the two for a
    single repo without changing MCP's read-only contract.
    """

    if not project_slug:
        raise ValueError("project_slug is required")
    if not repo_slug:
        raise ValueError("repo_slug is required")

    root = Path(repo_root).resolve()
    graph_path = root / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        raise FileNotFoundError(f"Graphify graph not found: {graph_path}")

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    raw_nodes = graph.get("nodes") or []
    raw_links = graph.get("links") or []

    nodes = [
        _normalize_node(n, project_slug=project_slug, repo_slug=repo_slug)
        for n in raw_nodes
    ]
    node_ids = {n["source_id"] for n in nodes}
    relations = [
        rel
        for rel in (
            _normalize_relation(
                link,
                project_slug=project_slug,
                repo_slug=repo_slug,
                _index=index,
            )
            for index, link in enumerate(raw_links)
        )
        if rel["source_id"] in node_ids and rel["target_id"] in node_ids
    ]

    repo = {
        "id": _repo_id(project_slug, repo_slug),
        "slug": repo_slug,
        "project_slug": project_slug,
        "root_path": str(root),
    }

    with neo4j_driver() as driver:
        with driver.session() as session:
            session.execute_write(_ensure_graphify_constraints)
            if clear_repo:
                session.execute_write(_clear_graphify_repo, repo["id"])
            session.execute_write(_upsert_graphify_project_repo, project_slug, repo)
            session.execute_write(_upsert_graphify_nodes, nodes)
            session.execute_write(_upsert_graphify_relations, relations)

    return GraphifySyncStats(
        project_slug=project_slug,
        repo_slug=repo_slug,
        nodes_written=len(nodes),
        relations_written=len(relations),
    )


def _repo_id(project_slug: str, repo_slug: str) -> str:
    return f"{project_slug}:{repo_slug}"


def _node_id(project_slug: str, repo_slug: str, source_id: str) -> str:
    return f"{project_slug}:{repo_slug}:{source_id}"


def _normalize_node(
    node: dict[str, Any], *, project_slug: str, repo_slug: str
) -> dict[str, Any]:
    source_id = str(node.get("id") or node.get("label") or "").strip()
    if not source_id:
        raise ValueError(f"Graphify node missing id/label: {node!r}")
    label = str(node.get("label") or source_id)
    return {
        "id": _node_id(project_slug, repo_slug, source_id),
        "source_id": source_id,
        "project_slug": project_slug,
        "repo_slug": repo_slug,
        "repo_id": _repo_id(project_slug, repo_slug),
        "label": label,
        "norm_label": str(node.get("norm_label") or label).lower(),
        "file_type": node.get("file_type"),
        "source_file": node.get("source_file"),
        "source_location": node.get("source_location"),
        "community": node.get("community"),
    }


def _normalize_relation(
    link: dict[str, Any],
    *,
    project_slug: str,
    repo_slug: str,
    _index: int,
) -> dict[str, Any]:
    source_id = str(link.get("source") or link.get("_src") or "")
    target_id = str(link.get("target") or link.get("_tgt") or "")
    relation = str(link.get("relation") or "related_to")
    confidence = str(link.get("confidence") or "EXTRACTED")
    confidence_score = link.get("confidence_score")
    if confidence_score is None:
        confidence_score = link.get("weight", 1.0)
    relation_id = _relation_id(
        project_slug=project_slug,
        repo_slug=repo_slug,
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        source_file=link.get("source_file"),
        source_location=link.get("source_location"),
    )
    return {
        "id": relation_id,
        "source_id": source_id,
        "target_id": target_id,
        "source_node_id": _node_id(project_slug, repo_slug, source_id),
        "target_node_id": _node_id(project_slug, repo_slug, target_id),
        "project_slug": project_slug,
        "repo_slug": repo_slug,
        "relation": relation,
        "confidence": confidence,
        "confidence_score": _float_or_zero(confidence_score),
        "source_file": link.get("source_file"),
        "source_location": link.get("source_location"),
    }


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _relation_id(
    *,
    project_slug: str,
    repo_slug: str,
    source_id: str,
    target_id: str,
    relation: str,
    source_file: Any,
    source_location: Any,
) -> str:
    payload = json.dumps(
        {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "source_file": source_file,
            "source_location": source_location,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{project_slug}:{repo_slug}:rel:{digest}"


def _ensure_graphify_constraints(tx) -> None:
    tx.run("""
        CREATE CONSTRAINT graphify_project_slug IF NOT EXISTS
        FOR (p:GraphifyProject) REQUIRE p.slug IS UNIQUE
        """)
    tx.run("""
        CREATE CONSTRAINT graphify_repo_id IF NOT EXISTS
        FOR (r:GraphifyRepo) REQUIRE r.id IS UNIQUE
        """)
    tx.run("""
        CREATE CONSTRAINT graphify_node_id IF NOT EXISTS
        FOR (n:GraphifyNode) REQUIRE n.id IS UNIQUE
        """)


def _clear_graphify_repo(tx, repo_id: str) -> None:
    tx.run(
        """
        MATCH (repo:GraphifyRepo {id: $repo_id})-[:HAS_GRAPH_NODE]->(node:GraphifyNode)
        DETACH DELETE node
        """,
        repo_id=repo_id,
    )
    tx.run(
        """
        MATCH (node:GraphifyNode {repo_id: $repo_id})
        DETACH DELETE node
        """,
        repo_id=repo_id,
    )


def _upsert_graphify_project_repo(tx, project_slug: str, repo: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (p:GraphifyProject {slug: $project_slug})
        SET p.source = 'graphify-out', p.updated_at = datetime()
        MERGE (r:GraphifyRepo {id: $repo.id})
        SET r.slug = $repo.slug,
            r.project_slug = $repo.project_slug,
            r.root_path = $repo.root_path,
            r.source = 'graphify-out',
            r.updated_at = datetime()
        MERGE (p)-[:HAS_REPO]->(r)
        """,
        project_slug=project_slug,
        repo=repo,
    )


def _upsert_graphify_nodes(tx, nodes: list[dict[str, Any]]) -> None:
    tx.run(
        """
        UNWIND $nodes AS node
        MATCH (r:GraphifyRepo {id: node.repo_id})
        MERGE (n:GraphifyNode {id: node.id})
        SET n.project_slug = node.project_slug,
            n.repo_slug = node.repo_slug,
            n.repo_id = node.repo_id,
            n.source_id = node.source_id,
            n.label = node.label,
            n.norm_label = node.norm_label,
            n.file_type = node.file_type,
            n.source_file = node.source_file,
            n.source_location = node.source_location,
            n.community = node.community,
            n.source = 'graphify-out',
            n.updated_at = datetime()
        MERGE (r)-[:HAS_GRAPH_NODE]->(n)
        """,
        nodes=nodes,
    )


def _upsert_graphify_relations(tx, relations: list[dict[str, Any]]) -> None:
    tx.run(
        """
        UNWIND $relations AS rel
        MATCH (a:GraphifyNode {id: rel.source_node_id})
        MATCH (b:GraphifyNode {id: rel.target_node_id})
        MERGE (a)-[r:GRAPHIFY_RELATION {id: rel.id}]->(b)
        SET r.project_slug = rel.project_slug,
            r.repo_slug = rel.repo_slug,
            r.relation = rel.relation,
            r.confidence = rel.confidence,
            r.confidence_score = rel.confidence_score,
            r.source_file = rel.source_file,
            r.source_location = rel.source_location,
            r.updated_at = datetime()
        """,
        relations=relations,
    )
