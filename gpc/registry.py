import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProjectResolutionError(LookupError):
    pass


def normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    if not slug or not SLUG_RE.match(slug):
        raise ValueError(f"Invalid slug: {value!r}")
    return slug


def normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def connect() -> psycopg.Connection:
    return psycopg.connect(POSTGRES_DSN, row_factory=dict_row)


def register_project(
    root_path: str | Path,
    *,
    slug: str | None = None,
    name: str | None = None,
    aliases: list[str] | None = None,
    description: str | None = None,
    primary_language: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root_path).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Project root does not exist or is not a directory: {root}")

    project_slug = normalize_slug(slug or root.name)
    project_name = name or root.name
    project_aliases = [project_slug, *(aliases or [])]

    with connect() as conn:
        project = conn.execute(
            """
            insert into gpc_projects (
                slug,
                name,
                root_path,
                description,
                primary_language,
                metadata
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (root_path) do update set
                slug = excluded.slug,
                name = excluded.name,
                description = coalesce(excluded.description, gpc_projects.description),
                primary_language = coalesce(
                    excluded.primary_language,
                    gpc_projects.primary_language
                ),
                metadata = gpc_projects.metadata || excluded.metadata
            returning *
            """,
            (
                project_slug,
                project_name,
                str(root),
                description,
                primary_language,
                Jsonb(metadata or {}),
            ),
        ).fetchone()

        for alias in project_aliases:
            _add_project_alias(conn, project["id"], alias)

        return _project_with_aliases(conn, project["id"])


def add_project_alias(project: str, alias: str, *, alias_type: str = "manual") -> dict[str, Any]:
    with connect() as conn:
        resolved = _resolve_by_slug_or_alias(conn, project)
        _add_project_alias(conn, resolved["id"], alias, alias_type=alias_type)
        return _project_with_aliases(conn, resolved["id"])


def register_source(
    root_path: str | Path,
    *,
    source_type: str,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root_path).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Source root does not exist or is not a directory: {root}")

    source_slug = normalize_slug(slug or f"{source_type}-{root.name}")
    source_name = name or root.name

    with connect() as conn:
        return conn.execute(
            """
            insert into gpc_sources (
                slug,
                name,
                source_type,
                root_path,
                description,
                metadata
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (root_path) do update set
                slug = excluded.slug,
                name = excluded.name,
                source_type = excluded.source_type,
                description = coalesce(excluded.description, gpc_sources.description),
                metadata = gpc_sources.metadata || excluded.metadata
            returning *
            """,
            (
                source_slug,
                source_name,
                source_type,
                str(root),
                description,
                Jsonb(metadata or {}),
            ),
        ).fetchone()


def link_project_source(
    project: str,
    source: str,
    *,
    role: str = "primary",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    role_slug = normalize_slug(role)

    with connect() as conn:
        resolved_project = _resolve_by_slug_or_alias(conn, project)
        resolved_source = _source_by_slug(conn, source)
        return conn.execute(
            """
            insert into gpc_project_sources (project_id, source_id, role, metadata)
            values (%s, %s, %s, %s)
            on conflict (project_id, source_id) do update set
                role = excluded.role,
                metadata = gpc_project_sources.metadata || excluded.metadata
            returning *
            """,
            (
                resolved_project["id"],
                resolved_source["id"],
                role_slug,
                Jsonb(metadata or {}),
            ),
        ).fetchone()


def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select id
            from gpc_projects
            order by slug
            """
        ).fetchall()
        return [_project_with_aliases(conn, row["id"]) for row in rows]


def resolve_project(
    *,
    cwd: str | Path | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        if cwd is not None:
            match = _resolve_by_cwd(conn, cwd)
            if match:
                match["resolution_reason"] = "cwd"
                return match

        if project:
            match = _resolve_by_slug_or_alias(conn, project)
            match["resolution_input"] = "project"
            return match

        if cwd is not None:
            config = find_gpc_config(cwd)
            configured_project = config.get("project") or config.get("slug")
            if configured_project:
                match = _resolve_by_slug_or_alias(conn, configured_project)
                match["resolution_reason"] = ".gpc.yaml"
                return match

    raise ProjectResolutionError(
        f"Could not resolve project from cwd={cwd!r}, project={project!r}"
    )


def find_gpc_config(start_path: str | Path) -> dict[str, Any]:
    path = Path(start_path).expanduser().resolve(strict=False)
    if path.is_file():
        path = path.parent

    for directory in [path, *path.parents]:
        config_path = directory / ".gpc.yaml"
        if config_path.exists():
            return _parse_gpc_config(config_path)

    return {}


def _add_project_alias(
    conn: psycopg.Connection,
    project_id: Any,
    alias: str,
    *,
    alias_type: str = "manual",
) -> dict[str, Any]:
    normalized_alias = normalize_slug(alias)
    normalized_type = normalize_slug(alias_type)

    existing = conn.execute(
        "select * from gpc_project_aliases where alias = %s",
        (normalized_alias,),
    ).fetchone()

    if existing and existing["project_id"] != project_id:
        raise ValueError(
            f"Alias {normalized_alias!r} already belongs to another project."
        )

    return conn.execute(
        """
        insert into gpc_project_aliases (project_id, alias, alias_type)
        values (%s, %s, %s)
        on conflict (alias) do update set
            alias_type = excluded.alias_type
        returning *
        """,
        (project_id, normalized_alias, normalized_type),
    ).fetchone()


def _resolve_by_slug_or_alias(conn: psycopg.Connection, value: str) -> dict[str, Any]:
    slug = normalize_slug(value)

    project = conn.execute(
        "select * from gpc_projects where slug = %s",
        (slug,),
    ).fetchone()
    if project:
        project = _project_with_aliases(conn, project["id"])
        project["resolution_reason"] = "slug"
        return project

    alias = conn.execute(
        """
        select project_id
        from gpc_project_aliases
        where alias = %s
        """,
        (slug,),
    ).fetchone()
    if alias:
        project = _project_with_aliases(conn, alias["project_id"])
        project["resolution_reason"] = "alias"
        return project

    raise ProjectResolutionError(f"Unknown project or alias: {value!r}")


def _resolve_by_cwd(conn: psycopg.Connection, cwd: str | Path) -> dict[str, Any] | None:
    current_path = Path(cwd).expanduser().resolve(strict=False)
    if current_path.is_file():
        current_path = current_path.parent

    projects = conn.execute(
        """
        select *
        from gpc_projects
        order by length(root_path) desc
        """
    ).fetchall()

    for project in projects:
        root = Path(project["root_path"]).expanduser().resolve(strict=False)
        try:
            current_path.relative_to(root)
        except ValueError:
            continue

        return _project_with_aliases(conn, project["id"])

    return None


def _project_with_aliases(conn: psycopg.Connection, project_id: Any) -> dict[str, Any]:
    project = conn.execute(
        "select * from gpc_projects where id = %s",
        (project_id,),
    ).fetchone()
    if not project:
        raise ProjectResolutionError(f"Project not found: {project_id}")

    aliases = conn.execute(
        """
        select alias
        from gpc_project_aliases
        where project_id = %s
        order by alias
        """,
        (project_id,),
    ).fetchall()
    project["aliases"] = [row["alias"] for row in aliases]
    return project


def _source_by_slug(conn: psycopg.Connection, value: str) -> dict[str, Any]:
    slug = normalize_slug(value)
    source = conn.execute(
        "select * from gpc_sources where slug = %s",
        (slug,),
    ).fetchone()
    if not source:
        raise LookupError(f"Unknown source: {value!r}")
    return source


def _parse_gpc_config(path: Path) -> dict[str, Any]:
    text = path.read_text()
    try:
        import yaml
    except ImportError:
        return _parse_simple_yaml(text)

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if current_list and line.startswith("- "):
            data.setdefault(current_list, []).append(line[2:].strip().strip("'\""))
            continue

        current_list = None
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if not value:
            current_list = key
            data[key] = []
        else:
            data[key] = value.strip("'\"")

    return data
