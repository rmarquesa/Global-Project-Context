"""Explicit, local, human-curated decision memory (migration 0011 required).

Only approved decisions with current *indexed* evidence are endorsed by default.
``source_revision`` is an optional caller label, NOT a verified Git revision.
No source files are read. Replaced file IDs fail closed, even at the same hash.
Reads never persist freshness or events. Terminal transitions and repeated
approve/invalidate requests are rejected, not silently made idempotent.

Writers lock the scoped repo first, then decisions and source evidence, in one
transaction. The partial unique index additionally protects against writers
that do not use this protocol. This is key-based, not semantic conflict detection.
The indexer's recognized-secret scanner is reused; it is not a perfect detector.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import PurePosixPath
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from gpc.db import pg_connection
from gpc.indexer import _looks_sensitive_content
from gpc.registry import ProjectResolutionError, resolve_repo


class DecisionError(ValueError):
    """Invalid input, scope, evidence, or lifecycle transition."""


class DecisionConflictError(DecisionError):
    """An explicit decision key already has an approved proposition."""


def _text(value: Any, name: str, maximum: int, *, secret: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > maximum
    ):
        raise DecisionError(
            f"{name} must be nonempty text of at most {maximum} characters"
        )
    if secret and _looks_sensitive_content(value):
        raise DecisionError(f"{name} contains recognized sensitive content")
    return value


def _optional_text(value: Any, name: str, maximum: int) -> str | None:
    return None if value is None else _text(value, name, maximum, secret=True)


def _path(value: Any) -> str:
    value = _text(value, "source_path", 512)
    if (
        value.startswith("/")
        or re.match(r"^[a-zA-Z]:", value)
        or "\\" in value
        or ".." in value.split("/")
    ):
        raise DecisionError(
            "source_path must be a relative POSIX path without traversal"
        )
    normalized = PurePosixPath(value).as_posix()
    if normalized == ".":
        raise DecisionError("source_path must name an indexed file")
    return normalized


def _id(value: Any, name: str = "decision_id") -> str:
    if not isinstance(value, str):
        raise DecisionError(f"{name} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise DecisionError(f"{name} must be a UUID string") from exc


def _scope(project: str, repo: str | None, *, write: bool = False) -> tuple[Any, Any]:
    _text(project, "project", 128)
    if repo is not None:
        _text(repo, "repo", 128)
    elif write:
        raise DecisionError("An explicit registered repo is required for writes")
    try:
        resolved = resolve_repo(project=project, repo=repo)
    except (ProjectResolutionError, ValueError) as exc:
        raise DecisionError("Project/repo could not be resolved") from exc
    project_id = resolved["project"]["id"]
    repository = resolved.get("repo")
    if repo is not None and (
        not repository or str(repository["project_id"]) != str(project_id)
    ):
        raise DecisionError("Repo does not belong to the requested project")
    return project_id, repository["id"] if repository else None


def _lock_repo(conn: Any, project_id: Any, repo_id: Any) -> None:
    # FIRST statement in every write transaction. Resolution used a separate
    # read transaction; recheck membership here to close that race.
    row = conn.execute(
        "select id from gpc_repos where project_id = %s and id = %s for update",
        (project_id, repo_id),
    ).fetchone()
    if not row:
        raise DecisionError("Registered repo no longer belongs to this project")


def _locked_decision(conn: Any, decision_id: str, project_id: Any, repo_id: Any):
    row = conn.execute(
        """select * from gpc_decisions
           where project_id = %s and repo_id = %s and id = %s for update""",
        (project_id, repo_id, decision_id),
    ).fetchone()
    if not row:
        raise DecisionError("Decision not found in the requested project/repo")
    return row


def _source(conn: Any, project_id: Any, repo_id: Any, path: str):
    row = conn.execute(
        """select id, content_hash, indexed_at from gpc_files
           where project_id = %s and repo_id = %s and relative_path = %s for share""",
        (project_id, repo_id, path),
    ).fetchone()
    if not row or not row["indexed_at"] or not (row["content_hash"] or "").strip():
        raise DecisionError("Source must have current indexed hash evidence")
    return row


def _event(conn: Any, decision_id: str, action: str, actor: str, reason: str | None):
    conn.execute(
        """insert into gpc_decision_events (id, decision_id, action, actor, reason)
           values (%s, %s, %s, %s, %s)""",
        (str(uuid4()), decision_id, action, actor, reason),
    )


_HYDRATE = """
    select d.*, r.slug as repo_slug,
           f.id as current_file_id, f.content_hash as current_source_hash,
           f.indexed_at as current_indexed_at
    from gpc_decisions d
    join gpc_repos r on r.id = d.repo_id and r.project_id = d.project_id
    left join gpc_files f on f.project_id = d.project_id and f.repo_id = d.repo_id
                         and f.relative_path = d.source_path
"""


def _json(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    file_id = result.pop("current_file_id")
    content_hash = result.pop("current_source_hash")
    indexed_at = result.pop("current_indexed_at")
    if file_id is None:
        freshness = "missing_source"
    elif (
        not result["source_file_id"]
        or not (result["source_hash"] or "").strip()
        or not (content_hash or "").strip()
        or not indexed_at
    ):
        freshness = "unverified"
    elif (
        str(file_id) != str(result["source_file_id"])
        or content_hash != result["source_hash"]
    ):
        freshness = "stale"
    else:
        freshness = "current"
    result["freshness"] = freshness
    result["key"] = result["decision_key"]
    return _json(result)


def _get(conn: Any, decision_id: str, project_id: Any, repo_id: Any):
    sql = _HYDRATE + " where d.project_id = %s and d.id = %s"
    params = [project_id, decision_id]
    if repo_id is not None:
        sql += " and d.repo_id = %s"
        params.append(repo_id)
    row = conn.execute(sql, params).fetchone()
    if not row:
        raise DecisionError("Decision not found in the requested project/repo")
    result = _serialize(row)
    result["events"] = _json(
        conn.execute(
            """select id, decision_id, action, actor, reason, created_at
               from gpc_decision_events where decision_id = %s
               order by created_at, id""",
            (decision_id,),
        ).fetchall()
    )
    return result


def capture_decision(
    *,
    project: str,
    repo: str,
    decision_key: str,
    title: str,
    content: str,
    source_path: str,
    actor: str,
    source_revision: str | None = None,
) -> dict:
    """Capture a pending proposition against indexed evidence, never autoapprove."""
    _text(decision_key, "decision_key", 80)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", decision_key):
        raise DecisionError("decision_key must be a strict lowercase slug")
    _text(title, "title", 240, secret=True)
    _text(content, "content", 8000, secret=True)
    _text(actor, "actor", 128, secret=True)
    source_path = _path(source_path)
    source_revision = _optional_text(source_revision, "source_revision", 128)
    project_id, repo_id = _scope(project, repo, write=True)
    decision_id = str(uuid4())
    with pg_connection(row_factory=dict_row) as conn:
        _lock_repo(conn, project_id, repo_id)
        evidence = _source(conn, project_id, repo_id, source_path)
        conn.execute(
            """insert into gpc_decisions
               (id, project_id, repo_id, decision_key, title, content, source_path,
                source_hash, source_revision, source_file_id, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')""",
            (
                decision_id,
                project_id,
                repo_id,
                decision_key,
                title,
                content,
                source_path,
                evidence["content_hash"],
                source_revision,
                evidence["id"],
            ),
        )
        _event(conn, decision_id, "captured", actor, None)
        return _get(conn, decision_id, project_id, repo_id)


def approve_decision(
    decision_id: str,
    *,
    project: str,
    repo: str,
    actor: str,
    reason: str | None = None,
    supersede: str | None = None,
) -> dict:
    """Approve current pending evidence; supersede only an exact scoped key ID."""
    decision_id = _id(decision_id)
    _text(actor, "actor", 128, secret=True)
    reason = _optional_text(reason, "reason", 2000)
    if supersede is not None:
        supersede = _id(supersede, "supersede")
    project_id, repo_id = _scope(project, repo, write=True)
    try:
        with pg_connection(row_factory=dict_row) as conn:
            _lock_repo(conn, project_id, repo_id)
            row = _locked_decision(conn, decision_id, project_id, repo_id)
            if row["status"] != "pending":
                raise DecisionError("Only pending decisions can be approved")
            evidence = _source(conn, project_id, repo_id, row["source_path"])
            if (
                str(evidence["id"]) != str(row["source_file_id"])
                or evidence["content_hash"] != row["source_hash"]
            ):
                raise DecisionError(
                    "Decision evidence is stale; capture a new decision"
                )
            current = conn.execute(
                """select id from gpc_decisions where project_id = %s and repo_id = %s
                   and source_path = %s and decision_key = %s
                   and status = 'approved' for update""",
                (project_id, repo_id, row["source_path"], row["decision_key"]),
            ).fetchone()
            if supersede is not None:
                old = _locked_decision(conn, supersede, project_id, repo_id)
                if (
                    old["status"] != "approved"
                    or old["source_path"] != row["source_path"]
                    or old["decision_key"] != row["decision_key"]
                    or not current
                    or str(current["id"]) != supersede
                ):
                    raise DecisionConflictError(
                        "supersede must name the exact approved scope/key"
                    )
            elif current:
                raise DecisionConflictError(
                    "Key already approved; explicit supersede ID required"
                )
            if supersede is not None:
                conn.execute(
                    """update gpc_decisions set status = 'superseded', superseded_by = %s,
                       updated_at = now() where id = %s""",
                    (decision_id, supersede),
                )
                _event(conn, supersede, "superseded", actor, reason)
            conn.execute(
                """update gpc_decisions set status = 'approved', approved_by = %s,
                   approved_at = now(), approval_reason = %s, decided_at = now(),
                   updated_at = now() where id = %s""",
                (actor, reason, decision_id),
            )
            _event(conn, decision_id, "approved", actor, reason)
            return _get(conn, decision_id, project_id, repo_id)
    except UniqueViolation as exc:
        raise DecisionConflictError(
            "Key already approved; explicit supersede ID required"
        ) from exc


def invalidate_decision(
    decision_id: str, *, project: str, repo: str, actor: str, reason: str
) -> dict:
    """Invalidate a pending/approved decision with a mandatory audit reason."""
    decision_id = _id(decision_id)
    _text(actor, "actor", 128, secret=True)
    _text(reason, "reason", 2000, secret=True)
    project_id, repo_id = _scope(project, repo, write=True)
    with pg_connection(row_factory=dict_row) as conn:
        _lock_repo(conn, project_id, repo_id)
        row = _locked_decision(conn, decision_id, project_id, repo_id)
        if row["status"] not in {"pending", "approved"}:
            raise DecisionError("Only pending or approved decisions can be invalidated")
        conn.execute(
            """update gpc_decisions set status = 'invalidated', updated_at = now()
               where id = %s""",
            (decision_id,),
        )
        _event(conn, decision_id, "invalidated", actor, reason)
        return _get(conn, decision_id, project_id, repo_id)


def get_decision(decision_id: str, *, project: str, repo: str | None = None) -> dict:
    """Read a scoped diagnostic, including audit events; never mutate freshness."""
    decision_id = _id(decision_id)
    project_id, repo_id = _scope(project, repo)
    with pg_connection(row_factory=dict_row) as conn:
        return _get(conn, decision_id, project_id, repo_id)


def list_decisions(
    *,
    project: str,
    repo: str | None = None,
    path: str | None = None,
    query: str | None = None,
    limit: int = 20,
    include_inactive: bool = False,
) -> list[dict]:
    """List approved/current decisions, or explicit inactive diagnostics.

    ``query`` is literal case-insensitive substring matching, not SQL wildcards.
    Project-only reads span registered repos in that project, never global rows.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise DecisionError("limit must be an integer from 1 to 100")
    if not isinstance(include_inactive, bool):
        raise DecisionError("include_inactive must be a boolean")
    if path is not None:
        path = _path(path)
    if query is not None:
        _text(query, "query", 512)
    project_id, repo_id = _scope(project, repo)
    sql = _HYDRATE + " where d.project_id = %s"
    params: list[Any] = [project_id]
    if repo_id is not None:
        sql += " and d.repo_id = %s"
        params.append(repo_id)
    if path is not None:
        sql += " and d.source_path = %s"
        params.append(path)
    if query is not None:
        literal = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        sql += """ and (lower(d.title) like lower(%s) escape '!'
                       or lower(d.content) like lower(%s) escape '!'
                       or lower(d.decision_key) like lower(%s) escape '!')"""
        params.extend([f"%{literal}%"] * 3)
    if not include_inactive:
        sql += """ and d.status = 'approved' and f.id = d.source_file_id
                   and f.content_hash = d.source_hash
                   and trim(f.content_hash) <> '' and f.indexed_at is not null"""
    sql += " order by d.created_at desc, d.id limit %s"
    params.append(limit)
    with pg_connection(row_factory=dict_row) as conn:
        return [_serialize(row) for row in conn.execute(sql, params).fetchall()]
