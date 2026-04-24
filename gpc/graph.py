from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gpc.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, POSTGRES_DSN


try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - exercised in environments without optional deps
    GraphDatabase = None


class Neo4jDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionStats:
    projects_written: int
    entities_written: int
    relations_written: int
    repos_written: int = 0


def postgres_connect() -> psycopg.Connection:
    return psycopg.connect(POSTGRES_DSN, row_factory=dict_row)


def neo4j_driver():
    if GraphDatabase is None:
        raise Neo4jDependencyError(
            "Missing neo4j Python driver. Install dependencies with "
            "`./venv/bin/pip install -r requirements.txt`."
        )

    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def ensure_neo4j_constraints() -> None:
    statements = [
        """
        create constraint gpc_project_id if not exists
        for (p:GPCProject)
        require p.id is unique
        """,
        """
        create constraint gpc_repo_id if not exists
        for (r:GPCRepo)
        require r.id is unique
        """,
        """
        create constraint gpc_entity_id if not exists
        for (e:GPCEntity)
        require e.id is unique
        """,
    ]

    with neo4j_driver() as driver:
        with driver.session() as session:
            for statement in statements:
                session.run(statement)


def project_graph_to_neo4j(projection_name: str = "default") -> ProjectionStats:
    projection_id = _start_projection(projection_name)

    try:
        ensure_neo4j_constraints()
        projects = _fetch_projects()
        repos = _fetch_repos()
        entities = _fetch_entities()
        relations = _fetch_relations()

        with neo4j_driver() as driver:
            with driver.session() as session:
                session.execute_write(_upsert_projects, projects)
                session.execute_write(_upsert_repos, repos)
                session.execute_write(_upsert_entities, entities)
                session.execute_write(_upsert_relations, relations)

        stats = ProjectionStats(
            projects_written=len(projects),
            entities_written=len(entities),
            relations_written=len(relations),
            repos_written=len(repos),
        )
        _finish_projection(projection_id, "succeeded", stats)
        return stats
    except Exception as exc:
        _fail_projection(projection_id, exc)
        raise


def neo4j_healthcheck() -> str:
    with neo4j_driver() as driver:
        with driver.session() as session:
            value = session.run("return 'ok' as status").single()["status"]
    return value


def _start_projection(projection_name: str) -> str:
    with postgres_connect() as conn:
        row = conn.execute(
            """
            insert into gpc_graph_projections (projection_name, status)
            values (%s, 'running')
            returning id
            """,
            (projection_name,),
        ).fetchone()
        return str(row["id"])


def _finish_projection(
    projection_id: str,
    status: str,
    stats: ProjectionStats,
) -> None:
    with postgres_connect() as conn:
        conn.execute(
            """
            update gpc_graph_projections
            set
                status = %s,
                finished_at = now(),
                projects_written = %s,
                entities_written = %s,
                relations_written = %s
            where id = %s
            """,
            (
                status,
                stats.projects_written,
                stats.entities_written,
                stats.relations_written,
                projection_id,
            ),
        )


def _fail_projection(projection_id: str, exc: Exception) -> None:
    with postgres_connect() as conn:
        conn.execute(
            """
            update gpc_graph_projections
            set
                status = 'failed',
                finished_at = now(),
                error_message = %s,
                metadata = metadata || %s
            where id = %s
            """,
            (
                str(exc),
                Jsonb({"error_type": exc.__class__.__name__}),
                projection_id,
            ),
        )


def _fetch_projects() -> list[dict[str, Any]]:
    with postgres_connect() as conn:
        return conn.execute(
            """
            select
                id::text as id,
                slug,
                name,
                root_path,
                description
            from gpc_projects
            order by slug
            """
        ).fetchall()


def _fetch_repos() -> list[dict[str, Any]]:
    with postgres_connect() as conn:
        return conn.execute(
            """
            select
                r.id::text as id,
                r.project_id::text as project_id,
                r.slug,
                r.name,
                r.root_path,
                r.description,
                p.slug as project_slug
            from gpc_repos r
            join gpc_projects p on p.id = r.project_id
            order by p.slug, r.slug
            """
        ).fetchall()


def _fetch_entities() -> list[dict[str, Any]]:
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select
                e.id::text as id,
                e.project_id::text as project_id,
                e.name,
                e.entity_type,
                e.external_ref,
                e.description,
                p.slug as project_slug,
                (e.metadata->>'repo_id')::text as repo_id,
                (e.metadata->>'repo_slug')::text as repo_slug,
                (e.metadata->>'relative_path')::text as relative_path,
                (e.metadata->>'language')::text as language,
                (e.metadata->>'file_type')::text as file_type
            from gpc_entities e
            left join gpc_projects p on p.id = e.project_id
            order by e.name
            """
        ).fetchall()
    return rows


def _fetch_relations() -> list[dict[str, Any]]:
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select
                r.id::text as id,
                r.project_id::text as project_id,
                r.source_entity_id::text as source_entity_id,
                r.target_entity_id::text as target_entity_id,
                r.relation_type,
                r.confidence,
                r.evidence_chunk_id::text as evidence_chunk_id
            from gpc_relations r
            order by r.created_at, r.id
            """
        ).fetchall()
    # Neo4j Bolt cannot encode Decimal — cast confidence to float eagerly.
    from decimal import Decimal

    for row in rows:
        if isinstance(row.get("confidence"), Decimal):
            row["confidence"] = float(row["confidence"])
    return rows


def _upsert_projects(tx, projects: list[dict[str, Any]]) -> None:
    tx.run(
        """
        unwind $projects as project
        merge (p:GPCProject {id: project.id})
        set
            p.slug = project.slug,
            p.name = project.name,
            p.root_path = project.root_path,
            p.description = project.description,
            p.updated_at = datetime()
        """,
        projects=projects,
    )


def _upsert_repos(tx, repos: list[dict[str, Any]]) -> None:
    if not repos:
        return
    tx.run(
        """
        unwind $repos as repo
        merge (r:GPCRepo {id: repo.id})
        set
            r.slug = repo.slug,
            r.name = repo.name,
            r.root_path = repo.root_path,
            r.description = repo.description,
            r.project_id = repo.project_id,
            r.project_slug = repo.project_slug,
            r.updated_at = datetime()
        with r, repo
        match (p:GPCProject {id: repo.project_id})
        merge (p)-[:OWNS_REPO]->(r)
        """,
        repos=repos,
    )


def _upsert_entities(tx, entities: list[dict[str, Any]]) -> None:
    tx.run(
        """
        unwind $entities as entity
        merge (e:GPCEntity {id: entity.id})
        set
            e.name = entity.name,
            e.entity_type = entity.entity_type,
            e.external_ref = entity.external_ref,
            e.description = entity.description,
            e.project_id = entity.project_id,
            e.project_slug = entity.project_slug,
            e.repo_id = entity.repo_id,
            e.repo_slug = entity.repo_slug,
            e.relative_path = entity.relative_path,
            e.language = entity.language,
            e.file_type = entity.file_type,
            e.updated_at = datetime()
        with e, entity
        match (p:GPCProject {id: entity.project_id})
        merge (p)-[:OWNS_ENTITY]->(e)
        with e, entity
        // Also attach to the owning repo when the entity metadata points at one.
        optional match (r:GPCRepo {id: entity.repo_id})
        foreach (_ in case when r is null then [] else [1] end |
            merge (r)-[:OWNS_ENTITY]->(e)
        )
        """,
        entities=[entity for entity in entities if entity.get("project_id")],
    )


def _upsert_relations(tx, relations: list[dict[str, Any]]) -> None:
    tx.run(
        """
        unwind $relations as relation
        match (source:GPCEntity {id: relation.source_entity_id})
        match (target:GPCEntity {id: relation.target_entity_id})
        merge (source)-[r:GPC_RELATION {id: relation.id}]->(target)
        set
            r.relation_type = relation.relation_type,
            r.project_id = relation.project_id,
            r.confidence = relation.confidence,
            r.evidence_chunk_id = relation.evidence_chunk_id,
            r.updated_at = datetime()
        """,
        relations=relations,
    )
