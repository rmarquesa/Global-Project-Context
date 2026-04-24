"""Light-weight entity/relation extractor for the GPC-side Neo4j projection.

The Graphify projection already captures rich structural relationships via
AST + LLM. This extractor is intentionally narrower and deterministic — it
only populates ``gpc_entities`` and ``gpc_relations`` with signals that are
cheap to compute from ``gpc_files``:

* every ``gpc_files`` row becomes a ``GPCEntity`` of type ``file`` so the
  GPC-side projection is no longer an empty shell;
* regex-detected ``import`` statements in JS/TS/Python create ``GPCRelation``
  rows (``relation_type = 'imports'``, ``confidence = 0.75`` INFERRED)
  between files that resolve to one another inside the same project.

That is enough to make ``project_graph_to_neo4j`` useful — callers can see
``(:GPCProject)-[:OWNS_REPO]->(:GPCRepo)-[:OWNS_ENTITY]->(:GPCEntity)``
with real contents, and cross-repo imports turn into cypher-queryable edges
with confidence tags. Future iterations can extend this without rewriting
the storage schema (classes, functions, doc headings, etc.).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gpc.config import POSTGRES_DSN


# One pattern per ecosystem. Keep them narrow to avoid false positives.
IMPORT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "javascript": [
        # import X from "./foo"
        re.compile(r"""(?m)^\s*import\s+[^;]*?\bfrom\s+['"]([^'"\n]+)['"]"""),
        # import "./foo"
        re.compile(r"""(?m)^\s*import\s+['"]([^'"\n]+)['"]"""),
        # require("./foo")
        re.compile(r"""require\(\s*['"]([^'"\n]+)['"]\s*\)"""),
    ],
    "typescript": [
        re.compile(r"""(?m)^\s*import\s+[^;]*?\bfrom\s+['"]([^'"\n]+)['"]"""),
        re.compile(r"""(?m)^\s*import\s+['"]([^'"\n]+)['"]"""),
        re.compile(r"""require\(\s*['"]([^'"\n]+)['"]\s*\)"""),
    ],
    "python": [
        # from foo.bar import baz
        re.compile(r"""(?m)^\s*from\s+([\w\.]+)\s+import\b"""),
        # import foo.bar
        re.compile(r"""(?m)^\s*import\s+([\w\.]+)"""),
    ],
}


JS_LANGUAGES = {"javascript", "typescript", "jsx", "tsx"}
PY_LANGUAGES = {"python"}

JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
INDEX_FILENAMES = ("index.js", "index.jsx", "index.ts", "index.tsx", "index.mjs")


def extract_for_project(project_id: str, project_slug: str) -> dict[str, int]:
    """Populate ``gpc_entities`` / ``gpc_relations`` from the current files.

    Idempotent: a second run updates the same ``gpc_entities`` rows (matched
    by ``(project_id, entity_type, name)``) and replaces ``imports``
    relations before re-creating them. ``metadata.source='gpc_indexer'`` is
    set on every row so future extractors can coexist safely.
    """

    stats = {"entities_upserted": 0, "relations_upserted": 0, "imports_parsed": 0}

    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        files = conn.execute(
            """
            select f.id::text as id,
                   f.relative_path,
                   f.absolute_path,
                   f.language,
                   f.file_type,
                   f.repo_id::text as repo_id,
                   r.slug as repo_slug
            from gpc_files f
            left join gpc_repos r on r.id = f.repo_id
            where f.project_id = %s
            order by f.relative_path
            """,
            (project_id,),
        ).fetchall()

        if not files:
            return stats

        # 1. Upsert one entity per file, using (repo_slug + relative_path)
        # as a stable name so repos with identical paths do not collide.
        entity_by_file: dict[str, str] = {}
        for file_row in files:
            entity_name = _entity_name(file_row)
            row = conn.execute(
                """
                insert into gpc_entities (project_id, name, entity_type, metadata)
                values (%s, %s, 'file', %s)
                on conflict (project_id, entity_type, name) do update set
                    metadata = gpc_entities.metadata || excluded.metadata
                returning id::text
                """,
                (
                    project_id,
                    entity_name,
                    Jsonb(
                        {
                            "source": "gpc_indexer",
                            "file_id": file_row["id"],
                            "relative_path": file_row["relative_path"],
                            "language": file_row["language"],
                            "file_type": file_row["file_type"],
                            "repo_id": file_row["repo_id"],
                            "repo_slug": file_row["repo_slug"],
                        }
                    ),
                ),
            ).fetchone()
            entity_by_file[file_row["id"]] = row["id"]
            stats["entities_upserted"] += 1

        # 2. Parse imports and link to the file they resolve to. Only files
        # in ecosystems we understand are parsed (JS/TS/Python).
        import_relations: list[tuple[str, str, dict[str, Any]]] = []
        for file_row in files:
            language = (file_row["language"] or "").lower()
            patterns = _patterns_for_language(language)
            if not patterns:
                continue
            try:
                text = Path(file_row["absolute_path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except (FileNotFoundError, PermissionError):
                continue

            targets = _extract_targets(text, patterns)
            stats["imports_parsed"] += len(targets)
            for target_spec in targets:
                resolved = _resolve_target(
                    target_spec=target_spec,
                    source_file=file_row,
                    files=files,
                    language=language,
                )
                if not resolved:
                    continue
                if resolved["id"] == file_row["id"]:
                    continue
                src_entity = entity_by_file[file_row["id"]]
                tgt_entity = entity_by_file[resolved["id"]]
                import_relations.append(
                    (
                        src_entity,
                        tgt_entity,
                        {
                            "target_spec": target_spec,
                            "from_file": file_row["relative_path"],
                            "to_file": resolved["relative_path"],
                            "from_repo": file_row["repo_slug"],
                            "to_repo": resolved["repo_slug"],
                        },
                    )
                )

        # 3. Replace imports relations for this project, then insert fresh
        # rows. This keeps reruns idempotent without needing a unique index
        # on relations (which 0001 does not define).
        conn.execute(
            """
            delete from gpc_relations
            where project_id = %s and relation_type = 'imports'
            """,
            (project_id,),
        )

        for src, tgt, meta in import_relations:
            if src == tgt:
                continue
            conn.execute(
                """
                insert into gpc_relations (
                    project_id,
                    source_entity_id,
                    target_entity_id,
                    relation_type,
                    confidence,
                    metadata
                )
                values (%s, %s, %s, 'imports', %s, %s)
                """,
                (
                    project_id,
                    src,
                    tgt,
                    0.75,
                    Jsonb({"source": "gpc_indexer", **meta}),
                ),
            )
            stats["relations_upserted"] += 1

    return stats


def _entity_name(file_row: dict[str, Any]) -> str:
    repo_slug = file_row.get("repo_slug") or "_noRepo"
    return f"{repo_slug}::{file_row['relative_path']}"


def _patterns_for_language(language: str) -> list[re.Pattern[str]]:
    if language in JS_LANGUAGES:
        return IMPORT_PATTERNS["javascript"]
    if language in PY_LANGUAGES:
        return IMPORT_PATTERNS["python"]
    return []


def _extract_targets(text: str, patterns: list[re.Pattern[str]]) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(pattern.findall(text))
    # dedup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for hit in hits:
        hit = hit.strip()
        if hit and hit not in seen:
            seen.add(hit)
            out.append(hit)
    return out


def _resolve_target(
    *,
    target_spec: str,
    source_file: dict[str, Any],
    files: list[dict[str, Any]],
    language: str,
) -> dict[str, Any] | None:
    """Return the file_row that the import resolves to, or None."""

    # Skip obvious externals: bare package names (no leading ./ or /).
    if language in JS_LANGUAGES:
        if not target_spec.startswith(".") and not target_spec.startswith("/"):
            return None
        return _resolve_js(target_spec, source_file, files)
    if language in PY_LANGUAGES:
        return _resolve_python(target_spec, source_file, files)
    return None


def _resolve_js(
    target_spec: str,
    source_file: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any] | None:
    source_dir = PurePosixPath(source_file["relative_path"]).parent
    normalized = PurePosixPath(target_spec)
    if target_spec.startswith("/"):
        candidate_path = normalized
    else:
        candidate_path = source_dir / normalized

    # Normalise "../" etc.
    candidate_str = str(candidate_path).replace("\\", "/")
    # Collapse .. by rebuilding through a PurePosixPath:
    parts: list[str] = []
    for part in candidate_str.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part in ("", "."):
            continue
        else:
            parts.append(part)
    candidate_str = "/".join(parts)

    # Try each plausible extension / index file, in order.
    by_path: dict[str, dict[str, Any]] = {
        f["relative_path"]: f for f in files if f.get("repo_slug") == source_file.get("repo_slug")
    }

    # Exact match first.
    if candidate_str in by_path:
        return by_path[candidate_str]

    for ext in JS_EXTENSIONS:
        full = f"{candidate_str}{ext}"
        if full in by_path:
            return by_path[full]

    for index_name in INDEX_FILENAMES:
        full = f"{candidate_str}/{index_name}"
        if full in by_path:
            return by_path[full]

    return None


def _resolve_python(
    target_spec: str,
    source_file: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any] | None:
    # Convert "foo.bar.baz" to "foo/bar/baz.py" and try both that and the
    # matching __init__.py inside the current repo only (imports across
    # repos happen but resolving them heuristically is low-signal).
    by_path: dict[str, dict[str, Any]] = {
        f["relative_path"]: f for f in files if f.get("repo_slug") == source_file.get("repo_slug")
    }
    candidate = "/".join(target_spec.split("."))
    direct = f"{candidate}.py"
    if direct in by_path:
        return by_path[direct]
    package = f"{candidate}/__init__.py"
    if package in by_path:
        return by_path[package]
    return None
