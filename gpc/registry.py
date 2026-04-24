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
    """Resolve a project following the documented priority order.

    Order (matches ``docs/architecture.md#project-resolution``):
        1. Explicit ``project`` slug or alias passed by the caller.
        2. ``cwd`` matched against registered project / repo roots
           (longest-prefix wins; repos checked first, then project roots).
        3. ``.gpc.yaml`` discovered by walking up from ``cwd``.

    Priority #1 before #2 is the important invariant. When an MCP client
    passes ``project="alugafacil"`` from a shell that happens to live in
    a different project's directory, the explicit slug must win — otherwise
    the resolver silently talks to the wrong project and the client can't
    override it without also passing a synthetic ``cwd``. ``resolve_repo``
    already obeyed this order; this brings ``resolve_project`` in line.
    """

    with connect() as conn:
        if project:
            match = _resolve_by_slug_or_alias(conn, project)
            match["resolution_input"] = "project"
            return match

        if cwd is not None:
            match = _resolve_by_cwd(conn, cwd)
            if match:
                match["resolution_reason"] = "cwd"
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


def ensure_project(
    *,
    slug: str,
    name: str | None = None,
    description: str | None = None,
    aliases: list[str] | None = None,
    root_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a logical project even when no single filesystem root exists.

    Use this to model "alugafacil has many repos" — the project row owns the
    repos, but the project's own ``root_path`` is synthetic and purely
    administrative. The root_path defaults to a safe placeholder that is
    unique per slug so the UNIQUE(root_path) constraint does not collide.
    """

    project_slug = normalize_slug(slug)
    project_name = name or slug
    placeholder = (
        str(Path(root_path).expanduser().resolve(strict=False))
        if root_path
        else f"virtual://gpc/projects/{project_slug}"
    )
    with connect() as conn:
        project = conn.execute(
            """
            insert into gpc_projects (slug, name, root_path, description)
            values (%s, %s, %s, %s)
            on conflict (slug) do update set
                name = coalesce(excluded.name, gpc_projects.name),
                description = coalesce(excluded.description, gpc_projects.description)
            returning *
            """,
            (project_slug, project_name, placeholder, description),
        ).fetchone()

        _add_project_alias(conn, project["id"], project_slug)
        for alias in aliases or []:
            _add_project_alias(conn, project["id"], alias)

        return _project_with_aliases(conn, project["id"])


def register_repo(
    project: str,
    root_path: str | Path,
    *,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
    create_project_if_missing: bool = False,
) -> dict[str, Any]:
    """Attach a repository to a project.

    ``project`` is a slug or alias. ``root_path`` must be an existing directory.
    Idempotent: re-registering the same root updates its metadata without
    creating duplicates.
    """

    root = Path(root_path).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Repo root does not exist or is not a directory: {root}")

    repo_slug = normalize_slug(slug or root.name)
    repo_name = name or root.name

    with connect() as conn:
        try:
            resolved_project = _resolve_by_slug_or_alias(conn, project)
        except ProjectResolutionError:
            if not create_project_if_missing:
                raise
            project_slug = normalize_slug(project)
            resolved_project = conn.execute(
                """
                insert into gpc_projects (slug, name, root_path)
                values (%s, %s, %s)
                on conflict (slug) do update set name = excluded.name
                returning *
                """,
                (project_slug, project_slug, f"virtual://gpc/projects/{project_slug}"),
            ).fetchone()
            _add_project_alias(conn, resolved_project["id"], project_slug)
            resolved_project = _project_with_aliases(conn, resolved_project["id"])

        repo = conn.execute(
            """
            insert into gpc_repos (project_id, slug, name, root_path, description, metadata)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (root_path) do update set
                project_id = excluded.project_id,
                slug = excluded.slug,
                name = coalesce(excluded.name, gpc_repos.name),
                description = coalesce(excluded.description, gpc_repos.description),
                metadata = gpc_repos.metadata || excluded.metadata
            returning *
            """,
            (
                resolved_project["id"],
                repo_slug,
                repo_name,
                str(root),
                description,
                Jsonb(metadata or {}),
            ),
        ).fetchone()
        repo["project_slug"] = resolved_project["slug"]
        repo["project_name"] = resolved_project["name"]
        return repo


def list_repos(project: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if project:
            resolved = _resolve_by_slug_or_alias(conn, project)
            rows = conn.execute(
                """
                select r.*, p.slug as project_slug, p.name as project_name
                from gpc_repos r
                join gpc_projects p on p.id = r.project_id
                where r.project_id = %s
                order by r.slug
                """,
                (resolved["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select r.*, p.slug as project_slug, p.name as project_name
                from gpc_repos r
                join gpc_projects p on p.id = r.project_id
                order by p.slug, r.slug
                """
            ).fetchall()
    return rows


def resolve_repo(
    *,
    cwd: str | Path | None = None,
    project: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Resolve a ``(project, repo)`` tuple.

    Resolution order mirrors ``resolve_project``:
        1. explicit ``project`` + optional ``repo`` slug.
        2. ``cwd`` matched against registered repo roots (longest prefix wins).
        3. ``cwd`` matched against registered project roots.
        4. ``.gpc.yaml`` discovered by walking up from ``cwd``.
    """

    with connect() as conn:
        if project:
            project_row = _resolve_by_slug_or_alias(conn, project)
            repo_row = None
            if repo:
                repo_slug = normalize_slug(repo)
                repo_row = conn.execute(
                    "select * from gpc_repos where project_id = %s and slug = %s",
                    (project_row["id"], repo_slug),
                ).fetchone()
                if not repo_row:
                    raise ProjectResolutionError(
                        f"Repo {repo!r} not found under project {project_row['slug']!r}"
                    )
            return {"project": project_row, "repo": repo_row, "resolution_reason": "explicit"}

        if cwd is not None:
            match = _resolve_repo_by_cwd(conn, cwd)
            if match:
                match["resolution_reason"] = "cwd"
                return match

        if cwd is not None:
            config = find_gpc_config(cwd)
            configured_project = config.get("project") or config.get("slug")
            configured_repo = config.get("repo")
            if configured_project:
                project_row = _resolve_by_slug_or_alias(conn, configured_project)
                repo_row = None
                if configured_repo:
                    repo_slug = normalize_slug(configured_repo)
                    repo_row = conn.execute(
                        "select * from gpc_repos where project_id = %s and slug = %s",
                        (project_row["id"], repo_slug),
                    ).fetchone()
                return {
                    "project": project_row,
                    "repo": repo_row,
                    "resolution_reason": ".gpc.yaml",
                }

    raise ProjectResolutionError(
        f"Could not resolve project/repo from cwd={cwd!r}, project={project!r}, repo={repo!r}"
    )


def _resolve_repo_by_cwd(
    conn: psycopg.Connection,
    cwd: str | Path,
) -> dict[str, Any] | None:
    current_path = Path(cwd).expanduser().resolve(strict=False)
    if current_path.is_file():
        current_path = current_path.parent

    repos = conn.execute(
        """
        select r.*, p.slug as project_slug, p.name as project_name, p.id as project_project_id
        from gpc_repos r
        join gpc_projects p on p.id = r.project_id
        order by length(r.root_path) desc
        """
    ).fetchall()

    for repo in repos:
        root = Path(repo["root_path"]).expanduser().resolve(strict=False)
        try:
            current_path.relative_to(root)
        except ValueError:
            continue
        project_row = _project_with_aliases(conn, repo["project_id"])
        return {"project": project_row, "repo": repo}

    project_only = _resolve_by_cwd(conn, cwd)
    if project_only:
        return {"project": project_only, "repo": None}
    return None


def consolidate_projects(
    target_slug: str,
    source_slugs: list[str],
    *,
    target_name: str | None = None,
    target_description: str | None = None,
    delete_source_projects: bool = False,
) -> dict[str, Any]:
    """Consolidate several standalone projects under a single logical project.

    Each ``source_slug`` becomes a repo under the target project, keeping its
    files, chunks, entities, relations, and decisions. Aliases are moved too.

    Set ``delete_source_projects=True`` to remove the now-empty source rows.
    """

    target_slug = normalize_slug(target_slug)
    source_slugs = [normalize_slug(s) for s in source_slugs]
    if not source_slugs:
        raise ValueError("consolidate_projects needs at least one source slug")
    if target_slug in source_slugs:
        raise ValueError("target slug must not appear in source slugs")

    stats: dict[str, Any] = {
        "target": target_slug,
        "sources": source_slugs,
        "repos_created": 0,
        "files_moved": 0,
        "chunks_moved": 0,
        "entities_moved": 0,
        "relations_moved": 0,
        "decisions_moved": 0,
        "aliases_moved": 0,
        "source_projects_deleted": 0,
    }

    with connect() as conn:
        target = conn.execute(
            """
            insert into gpc_projects (slug, name, root_path, description)
            values (%s, %s, %s, %s)
            on conflict (slug) do update set
                name = coalesce(excluded.name, gpc_projects.name),
                description = coalesce(excluded.description, gpc_projects.description)
            returning *
            """,
            (
                target_slug,
                target_name or target_slug,
                f"virtual://gpc/projects/{target_slug}",
                target_description,
            ),
        ).fetchone()
        _add_project_alias(conn, target["id"], target_slug)
        target_id = target["id"]

        for source_slug in source_slugs:
            source = conn.execute(
                "select * from gpc_projects where slug = %s",
                (source_slug,),
            ).fetchone()
            if not source:
                raise ProjectResolutionError(f"Source project not found: {source_slug!r}")

            repo = conn.execute(
                """
                insert into gpc_repos (project_id, slug, name, root_path, description, metadata)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (root_path) do update set
                    project_id = excluded.project_id,
                    slug = excluded.slug,
                    name = coalesce(excluded.name, gpc_repos.name),
                    description = coalesce(excluded.description, gpc_repos.description)
                returning id
                """,
                (
                    target_id,
                    source_slug,
                    source["name"],
                    source["root_path"],
                    source["description"],
                    Jsonb({"consolidated_from_project": source_slug}),
                ),
            ).fetchone()
            stats["repos_created"] += 1
            repo_id = repo["id"]

            stats["files_moved"] += conn.execute(
                "update gpc_files set project_id = %s, repo_id = %s where project_id = %s",
                (target_id, repo_id, source["id"]),
            ).rowcount or 0
            stats["chunks_moved"] += conn.execute(
                "update gpc_chunks set project_id = %s, repo_id = %s where project_id = %s",
                (target_id, repo_id, source["id"]),
            ).rowcount or 0
            stats["entities_moved"] += conn.execute(
                "update gpc_entities set project_id = %s where project_id = %s",
                (target_id, source["id"]),
            ).rowcount or 0
            stats["relations_moved"] += conn.execute(
                "update gpc_relations set project_id = %s where project_id = %s",
                (target_id, source["id"]),
            ).rowcount or 0
            stats["decisions_moved"] += conn.execute(
                "update gpc_decisions set project_id = %s where project_id = %s",
                (target_id, source["id"]),
            ).rowcount or 0
            stats["aliases_moved"] += conn.execute(
                """
                update gpc_project_aliases set project_id = %s
                where project_id = %s
                """,
                (target_id, source["id"]),
            ).rowcount or 0
            # The source slug itself should remain resolvable as an alias of
            # the target. If the alias row was not there (e.g. it was never
            # registered explicitly), create it now.
            conn.execute(
                """
                insert into gpc_project_aliases (project_id, alias, alias_type)
                values (%s, %s, 'consolidated')
                on conflict (alias) do nothing
                """,
                (target_id, source_slug),
            )

            if delete_source_projects:
                conn.execute("delete from gpc_projects where id = %s", (source["id"],))
                stats["source_projects_deleted"] += 1

    return stats


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
