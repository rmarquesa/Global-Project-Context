from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, PointStruct

from gpc.config import COLLECTION_NAME, POSTGRES_DSN, QDRANT_HOST, QDRANT_PORT
from gpc.embeddings import embed_texts
from gpc.registry import (
    link_project_source,
    register_project,
    register_repo,
    register_source,
)


IGNORED_DIRS = {
    ".git",
    ".gpc",
    ".hg",
    ".svn",
    ".cache",
    ".codex",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "data",
    "dist",
    "graphify-out",
    "node_modules",
    "target",
    "venv",
}

IGNORED_FILES = {
    ".DS_Store",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

SAFE_ENV_EXAMPLES = {
    ".env.example",
    ".env.sample",
    ".env.template",
}

SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}

SENSITIVE_EXTENSIONS = {
    ".crt",
    ".der",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
}

SENSITIVE_NAME_PATTERNS = (
    "*.secret",
    "*.secrets",
    "*_secret",
    "*_secrets",
    "secrets.*",
)

SENSITIVE_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{24,}"
    ),
)

BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dmg",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".tar",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}

LANGUAGE_BY_EXTENSION = {
    ".bash": "shell",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mdx": "mdx",
    ".php": "php",
    ".prisma": "prisma",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".svelte": "svelte",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "shell",
}

CODE_EXTENSIONS = {
    ".bash",
    ".c",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".lua",
    ".php",
    ".prisma",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".zsh",
}

DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt"}
CONFIG_EXTENSIONS = {".json", ".toml", ".yaml", ".yml", ".xml"}
SUPPORTED_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS | CONFIG_EXTENSIONS | {".css", ".html", ".sql"}
SUPPORTED_FILENAMES = {
    ".dockerignore",
    ".env.example",
    ".env.sample",
    ".env.template",
    ".gitignore",
    ".gpc.yaml",
    "AGENTS.md",
    "Dockerfile",
    "Makefile",
}


@dataclass(frozen=True)
class IndexOptions:
    reset: bool = False
    force: bool = False
    prune_deleted: bool = True
    fail_fast: bool = False
    include_unknown_text: bool = False
    follow_symlinks: bool = False
    max_file_bytes: int = 512_000
    chunk_chars: int = 3_500
    batch_size: int = 16
    limit_files: int | None = None


@dataclass(frozen=True)
class FileCandidate:
    absolute_path: Path
    relative_path: str
    size_bytes: int
    language: str | None
    file_type: str


@dataclass(frozen=True)
class TextChunk:
    index: int
    title: str
    content: str
    content_hash: str
    token_count: int
    chunk_type: str


@dataclass(frozen=True)
class FileIndexResult:
    status: str
    chunks_written: int = 0
    chunks_deleted: int = 0
    points_deleted: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: list[FileCandidate]
    skipped: Counter[str]
    mode: str


@dataclass(frozen=True)
class IndexStats:
    project_slug: str
    project_id: str
    discovery_mode: str
    files_discovered: int
    files_seen: int
    files_indexed: int
    files_unchanged: int
    files_skipped: int
    files_failed: int
    chunks_written: int
    chunks_deleted: int
    points_deleted: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def index_project_path(
    root_path: str | Path,
    *,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    options: IndexOptions | None = None,
    repo_slug: str | None = None,
) -> IndexStats:
    opts = options or IndexOptions()
    root = Path(root_path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)

    # When indexing via --project <parent>, the caller passes the parent
    # project slug and the repo slug explicitly. We do NOT call
    # register_project here because doing so would change the project's own
    # root_path and fight with its virtual placeholder.
    if repo_slug:
        repo = register_repo(
            slug,
            root,
            slug=repo_slug,
            name=name,
            description=description,
            create_project_if_missing=False,
        )
        project = {
            "id": repo["project_id"],
            "slug": repo["project_slug"],
            "name": repo.get("project_name") or repo["project_slug"],
            "root_path": repo["root_path"],
        }
    else:
        project = register_project(
            root,
            slug=slug,
            name=name,
            description=description,
            metadata={"indexed_by": "gpc_indexer"},
        )
        repo = register_repo(
            project["slug"],
            root,
            slug=project["slug"],
            name=project["name"],
            description=description,
        )
    source = register_source(
        root,
        source_type="project_root",
        slug=f"{project['slug']}-{repo['slug']}-root"[:60],
        name=f"{project['name']} root ({repo['slug']})",
        metadata={"indexed_by": "gpc_indexer", "repo_slug": repo["slug"]},
    )
    link_project_source(project["slug"], source["slug"], role="primary")
    project["_repo_id"] = repo["id"]
    project["_repo_slug"] = repo["slug"]

    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    run_id = _start_index_run(project["id"], opts)

    try:
        if opts.reset:
            _clear_project_index(project, qdrant)

        discovery = discover_project_files(root, opts)
        skipped_reasons = Counter(discovery.skipped)
        chunks_deleted = 0
        points_deleted = 0

        if opts.prune_deleted and opts.limit_files is None:
            pruned = _prune_missing_files(
                project,
                {candidate.relative_path for candidate in discovery.candidates},
                qdrant,
            )
            chunks_deleted += pruned.chunks_deleted
            points_deleted += pruned.points_deleted

        files_indexed = 0
        files_unchanged = 0
        files_failed = 0
        chunks_written = 0
        errors: list[str] = []

        for candidate in discovery.candidates:
            try:
                result = _index_file(project, candidate, qdrant, opts)
            except Exception as exc:
                files_failed += 1
                errors.append(f"{candidate.relative_path}: {exc}")
                if opts.fail_fast:
                    raise
                continue

            chunks_deleted += result.chunks_deleted
            points_deleted += result.points_deleted
            if result.status == "indexed":
                files_indexed += 1
                chunks_written += result.chunks_written
            elif result.status == "unchanged":
                files_unchanged += 1
            elif result.status == "skipped":
                skipped_reasons[result.reason or "skipped"] += 1

        files_skipped = sum(skipped_reasons.values())

        # Light-weight GPC-side entity/relation extraction. Populates
        # gpc_entities/gpc_relations so project_graph_to_neo4j has real
        # content to project beyond (:GPCProject)-[:OWNS_REPO]->(:GPCRepo).
        try:
            from gpc.entity_extractor import extract_for_project

            extract_for_project(str(project["id"]), project["slug"])
        except Exception as exc:  # noqa: BLE001 — never block the index run
            errors.append(f"entity_extractor: {exc}")

        # Longitudinal metrics snapshot — drives the drift detector (Fase 3).
        try:
            from gpc.self_metrics import collect_metrics

            collect_metrics(project_slug=project["slug"], source="gpc-index")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"self_metrics: {exc}")

        stats = IndexStats(
            project_slug=project["slug"],
            project_id=str(project["id"]),
            discovery_mode=discovery.mode,
            files_discovered=len(discovery.candidates) + sum(discovery.skipped.values()),
            files_seen=len(discovery.candidates),
            files_indexed=files_indexed,
            files_unchanged=files_unchanged,
            files_skipped=files_skipped,
            files_failed=files_failed,
            chunks_written=chunks_written,
            chunks_deleted=chunks_deleted,
            points_deleted=points_deleted,
            skipped_reasons=dict(skipped_reasons),
            errors=errors[:20],
        )
        _finish_index_run(run_id, stats)
        return stats
    except Exception as exc:
        _fail_index_run(run_id, exc)
        raise


def discover_project_files(root: Path, options: IndexOptions) -> DiscoveryResult:
    if _is_git_worktree(root):
        paths = _git_discover_paths(root)
        if paths:
            return _build_discovery(root, paths, options, mode="git")

    return _walk_discover_files(root, options)


def _index_file(
    project: dict[str, Any],
    candidate: FileCandidate,
    qdrant: QdrantClient,
    options: IndexOptions,
) -> FileIndexResult:
    raw = candidate.absolute_path.read_bytes()
    if b"\x00" in raw:
        removed = _remove_file_index(project["id"], candidate.relative_path, qdrant)
        return FileIndexResult(
            status="skipped",
            reason="binary_content",
            chunks_deleted=removed.chunks_deleted,
            points_deleted=removed.points_deleted,
        )

    text = raw.decode("utf-8", errors="replace")
    if _looks_sensitive_content(text):
        removed = _remove_file_index(project["id"], candidate.relative_path, qdrant)
        return FileIndexResult(
            status="skipped",
            reason="sensitive_content",
            chunks_deleted=removed.chunks_deleted,
            points_deleted=removed.points_deleted,
        )

    file_hash = hashlib.sha256(raw).hexdigest()
    with _connect() as conn:
        existing = conn.execute(
            """
            select id, content_hash, indexed_at
            from gpc_files
            where project_id = %s and relative_path = %s
            """,
            (project["id"], candidate.relative_path),
        ).fetchone()
        if (
            not options.force
            and existing
            and existing["content_hash"] == file_hash
            and existing["indexed_at"] is not None
        ):
            return FileIndexResult(status="unchanged")

    chunks = chunk_text(
        text,
        relative_path=candidate.relative_path,
        chunk_type=_chunk_type(candidate.absolute_path),
        max_chars=options.chunk_chars,
    )
    if not chunks:
        removed = _remove_file_index(project["id"], candidate.relative_path, qdrant)
        return FileIndexResult(
            status="skipped",
            reason="empty_text",
            chunks_deleted=removed.chunks_deleted,
            points_deleted=removed.points_deleted,
        )

    embeddings = []
    embedding_provider = "unknown"
    embedding_model = "unknown"
    for start in range(0, len(chunks), options.batch_size):
        batch_chunks = chunks[start : start + options.batch_size]
        batch = embed_texts([chunk.content for chunk in batch_chunks])
        embeddings.extend(batch.vectors)
        embedding_provider = batch.provider
        embedding_model = batch.model

    with _connect() as conn:
        with conn.transaction():
            file_row = _upsert_file(conn, project, candidate, file_hash)
            old_point_ids = _delete_existing_chunks(conn, file_row["id"])
            chunk_rows = _insert_chunks(
                conn,
                project,
                file_row,
                candidate,
                chunks,
                embedding_model,
            )

    points = []
    new_point_ids = []
    for chunk_row, chunk, vector in zip(chunk_rows, chunks, embeddings, strict=True):
        point_id = chunk_row["qdrant_point_id"]
        new_point_ids.append(point_id)
        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "project_id": str(project["id"]),
                    "project_slug": project["slug"],
                    "project_name": project["name"],
                    "root_path": project["root_path"],
                    "repo_id": str(project["_repo_id"]) if project.get("_repo_id") else None,
                    "repo_slug": project.get("_repo_slug"),
                    "file_id": str(chunk_row["file_id"]),
                    "chunk_id": str(chunk_row["id"]),
                    "relative_path": candidate.relative_path,
                    "language": candidate.language,
                    "file_type": candidate.file_type,
                    "source_type": "project_file",
                    "chunk_type": chunk.chunk_type,
                    "chunk_index": chunk.index,
                    "content_hash": chunk.content_hash,
                    "file_hash": file_hash,
                    "embedding_provider": embedding_provider,
                    "embedding_model": embedding_model,
                    "embedding_dimensions": len(vector),
                    "title": chunk.title,
                },
            )
        )

    qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
    stale_point_ids = sorted(set(old_point_ids) - set(new_point_ids))
    if stale_point_ids:
        qdrant.delete(collection_name=COLLECTION_NAME, points_selector=stale_point_ids)

    with _connect() as conn:
        conn.execute(
            """
            update gpc_files
            set indexed_at = now()
            where id = %s
            """,
            (chunk_rows[0]["file_id"],),
        )

    return FileIndexResult(
        status="indexed",
        chunks_written=len(chunks),
        chunks_deleted=len(old_point_ids),
        points_deleted=len(stale_point_ids),
    )


def chunk_text(
    text: str,
    *,
    relative_path: str,
    chunk_type: str,
    max_chars: int,
) -> list[TextChunk]:
    clean_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean_text:
        return []

    chunks: list[TextChunk] = []
    current: list[str] = []
    current_size = 0

    for paragraph in _paragraphs(clean_text):
        if len(paragraph) > max_chars:
            if current:
                _append_chunk(chunks, relative_path, chunk_type, current)
                current = []
                current_size = 0
            for start in range(0, len(paragraph), max_chars):
                _append_chunk(chunks, relative_path, chunk_type, [paragraph[start : start + max_chars]])
            continue

        if current and current_size + len(paragraph) > max_chars:
            _append_chunk(chunks, relative_path, chunk_type, current)
            current = []
            current_size = 0

        current.append(paragraph)
        current_size += len(paragraph)

    if current:
        _append_chunk(chunks, relative_path, chunk_type, current)

    return chunks


def _paragraphs(text: str) -> list[str]:
    blocks = re.split(r"(\n\s*\n)", text)
    paragraphs = []
    for index in range(0, len(blocks), 2):
        paragraph = blocks[index]
        separator = blocks[index + 1] if index + 1 < len(blocks) else ""
        if paragraph.strip():
            paragraphs.append(paragraph + separator)
    return paragraphs or [text]


def _append_chunk(
    chunks: list[TextChunk],
    relative_path: str,
    chunk_type: str,
    parts: list[str],
) -> None:
    content = "".join(parts).strip()
    if not content:
        return

    index = len(chunks)
    content_hash = hashlib.sha256(
        f"{relative_path}\n{index}\n{content}".encode("utf-8")
    ).hexdigest()
    chunks.append(
        TextChunk(
            index=index,
            title=f"{relative_path}#{index}",
            content=content,
            content_hash=content_hash,
            token_count=len(content.split()),
            chunk_type=chunk_type,
        )
    )


def _build_discovery(
    root: Path,
    paths: list[Path],
    options: IndexOptions,
    *,
    mode: str,
) -> DiscoveryResult:
    skipped: Counter[str] = Counter()
    candidates: list[FileCandidate] = []

    for absolute_path in paths:
        candidate = _candidate_from_path(root, absolute_path, options, skipped)
        if not candidate:
            continue

        candidates.append(candidate)
        if options.limit_files and len(candidates) >= options.limit_files:
            break

    return DiscoveryResult(candidates=candidates, skipped=skipped, mode=mode)


def _walk_discover_files(root: Path, options: IndexOptions) -> DiscoveryResult:
    skipped: Counter[str] = Counter()
    candidates: list[FileCandidate] = []
    gitignore_patterns = _load_gitignore_patterns(root)

    for current_root, dirnames, filenames in os.walk(root, followlinks=options.follow_symlinks):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(root)
        kept_dirs = []
        for dirname in sorted(dirnames):
            rel_path = rel_dir / dirname
            if _should_ignore_dir(rel_path, dirname, gitignore_patterns):
                skipped["ignored_dir"] += 1
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            absolute_path = current_path / filename
            candidate = _candidate_from_path(root, absolute_path, options, skipped)
            if not candidate:
                continue

            candidates.append(candidate)
            if options.limit_files and len(candidates) >= options.limit_files:
                return DiscoveryResult(candidates=candidates, skipped=skipped, mode="walk")

    return DiscoveryResult(candidates=candidates, skipped=skipped, mode="walk")


def _candidate_from_path(
    root: Path,
    absolute_path: Path,
    options: IndexOptions,
    skipped: Counter[str],
) -> FileCandidate | None:
    try:
        if not absolute_path.exists() or not absolute_path.is_file():
            skipped["not_file"] += 1
            return None
        if absolute_path.is_symlink() and not options.follow_symlinks:
            skipped["symlink"] += 1
            return None
        stat = absolute_path.stat()
    except OSError:
        skipped["stat_error"] += 1
        return None

    relative_path = absolute_path.relative_to(root).as_posix()
    filename = absolute_path.name
    suffix = absolute_path.suffix.lower()

    if filename in IGNORED_FILES:
        skipped["ignored_file"] += 1
        return None
    if _looks_sensitive_filename(filename):
        skipped["sensitive_filename"] += 1
        return None
    if suffix in BINARY_EXTENSIONS:
        skipped["binary_extension"] += 1
        return None
    if stat.st_size > options.max_file_bytes:
        skipped["too_large"] += 1
        return None
    if not options.include_unknown_text and not _is_supported_text_path(absolute_path):
        skipped["unsupported_extension"] += 1
        return None

    return FileCandidate(
        absolute_path=absolute_path,
        relative_path=relative_path,
        size_bytes=stat.st_size,
        language=_language_for_path(absolute_path),
        file_type=_file_type(absolute_path),
    )


def _is_git_worktree(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_discover_paths(root: Path) -> list[Path]:
    try:
        top_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        repo_root = Path(top_result.stdout.strip()).resolve(strict=True)
        rel_root = root.relative_to(repo_root).as_posix()
        command = [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ]
        if rel_root != ".":
            command.extend(["--", rel_root])
        files_result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    paths = []
    for raw_path in files_result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not raw_path:
            continue
        absolute_path = (repo_root / raw_path).resolve(strict=False)
        try:
            absolute_path.relative_to(root)
        except ValueError:
            continue
        paths.append(absolute_path)

    return sorted(set(paths))


def _upsert_file(
    conn: psycopg.Connection,
    project: dict[str, Any],
    candidate: FileCandidate,
    file_hash: str,
) -> dict[str, Any]:
    repo_id = project.get("_repo_id")
    return conn.execute(
        """
        insert into gpc_files (
            project_id,
            repo_id,
            relative_path,
            absolute_path,
            language,
            file_type,
            size_bytes,
            content_hash,
            metadata
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (project_id, coalesce(repo_id, '00000000-0000-0000-0000-000000000000'::uuid), relative_path) do update set
            repo_id = coalesce(excluded.repo_id, gpc_files.repo_id),
            absolute_path = excluded.absolute_path,
            language = excluded.language,
            file_type = excluded.file_type,
            size_bytes = excluded.size_bytes,
            content_hash = excluded.content_hash,
            metadata = gpc_files.metadata || excluded.metadata,
            indexed_at = null
        returning *
        """,
        (
            project["id"],
            repo_id,
            candidate.relative_path,
            str(candidate.absolute_path),
            candidate.language,
            candidate.file_type,
            candidate.size_bytes,
            file_hash,
            Jsonb({"indexed_by": "gpc_indexer"}),
        ),
    ).fetchone()


def _delete_existing_chunks(conn: psycopg.Connection, file_id: Any) -> list[str]:
    rows = conn.execute(
        """
        select qdrant_point_id
        from gpc_chunks
        where file_id = %s and qdrant_point_id is not null
        """,
        (file_id,),
    ).fetchall()
    conn.execute("delete from gpc_chunks where file_id = %s", (file_id,))
    return [row["qdrant_point_id"] for row in rows]


def _insert_chunks(
    conn: psycopg.Connection,
    project: dict[str, Any],
    file_row: dict[str, Any],
    candidate: FileCandidate,
    chunks: list[TextChunk],
    embedding_model: str,
) -> list[dict[str, Any]]:
    rows = []
    repo_id = project.get("_repo_id") or file_row.get("repo_id")
    repo_slug = project.get("_repo_slug")
    for chunk in chunks:
        chunk_id = uuid5(
            NAMESPACE_URL,
            f"gpc/chunk/{project['id']}/{repo_id or 'no-repo'}/{chunk.content_hash}",
        )
        row = conn.execute(
            """
            insert into gpc_chunks (
                id,
                project_id,
                repo_id,
                file_id,
                source_type,
                chunk_type,
                chunk_index,
                title,
                content,
                content_hash,
                token_count,
                qdrant_collection,
                qdrant_point_id,
                embedding_model,
                metadata
            )
            values (%s, %s, %s, %s, 'project_file', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning *
            """,
            (
                chunk_id,
                project["id"],
                repo_id,
                file_row["id"],
                chunk.chunk_type,
                chunk.index,
                chunk.title,
                chunk.content,
                chunk.content_hash,
                chunk.token_count,
                COLLECTION_NAME,
                str(chunk_id),
                embedding_model,
                Jsonb(
                    {
                        "relative_path": candidate.relative_path,
                        "language": candidate.language,
                        "file_type": candidate.file_type,
                        "repo_slug": repo_slug,
                    }
                ),
            ),
        ).fetchone()
        rows.append(row)
    return rows


def _clear_project_index(project: dict[str, Any], qdrant: QdrantClient) -> FileIndexResult:
    qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[
                FieldCondition(
                    key="project_id",
                    match=MatchValue(value=str(project["id"])),
                )
            ]
        ),
    )
    with _connect() as conn:
        rows = conn.execute(
            """
            select count(*) as file_count
            from gpc_files
            where project_id = %s
            """,
            (project["id"],),
        ).fetchone()
        conn.execute("delete from gpc_files where project_id = %s", (project["id"],))
    return FileIndexResult(status="indexed", chunks_deleted=0, points_deleted=0)


def _remove_file_index(
    project_id: Any,
    relative_path: str,
    qdrant: QdrantClient,
) -> FileIndexResult:
    with _connect() as conn:
        rows = conn.execute(
            """
            select c.qdrant_point_id
            from gpc_files f
            left join gpc_chunks c on c.file_id = f.id
            where f.project_id = %s
              and f.relative_path = %s
              and c.qdrant_point_id is not null
            """,
            (project_id, relative_path),
        ).fetchall()
        point_ids = [row["qdrant_point_id"] for row in rows]

    if point_ids:
        qdrant.delete(collection_name=COLLECTION_NAME, points_selector=point_ids)

    with _connect() as conn:
        conn.execute(
            """
            delete from gpc_files
            where project_id = %s and relative_path = %s
            """,
            (project_id, relative_path),
        )

    return FileIndexResult(
        status="indexed",
        chunks_deleted=len(point_ids),
        points_deleted=len(point_ids),
    )


def _prune_missing_files(
    project: dict[str, Any],
    discovered_paths: set[str],
    qdrant: QdrantClient,
) -> FileIndexResult:
    discovered = sorted(discovered_paths)
    repo_id = project.get("_repo_id")
    with _connect() as conn:
        if repo_id:
            # When a repo is in scope, prune only within that repo so parallel
            # repos of the same project do not wipe each other's index.
            rows = conn.execute(
                """
                select f.id, c.qdrant_point_id
                from gpc_files f
                left join gpc_chunks c on c.file_id = f.id
                where f.project_id = %s
                  and f.repo_id = %s
                  and not (f.relative_path = any(%s::text[]))
                """,
                (project["id"], repo_id, discovered),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select f.id, c.qdrant_point_id
                from gpc_files f
                left join gpc_chunks c on c.file_id = f.id
                where f.project_id = %s
                  and not (f.relative_path = any(%s::text[]))
                """,
                (project["id"], discovered),
            ).fetchall()

    file_ids = sorted({row["id"] for row in rows})
    point_ids = [row["qdrant_point_id"] for row in rows if row["qdrant_point_id"]]

    if point_ids:
        qdrant.delete(collection_name=COLLECTION_NAME, points_selector=point_ids)

    if file_ids:
        with _connect() as conn:
            conn.execute("delete from gpc_files where id = any(%s::uuid[])", (file_ids,))

    return FileIndexResult(
        status="indexed" if file_ids else "unchanged",
        chunks_deleted=len(point_ids),
        points_deleted=len(point_ids),
    )


def _start_index_run(project_id: Any, options: IndexOptions) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            insert into gpc_index_runs (project_id, status, metadata)
            values (%s, 'running', %s)
            returning id
            """,
            (
                project_id,
                Jsonb(_options_metadata(options)),
            ),
        ).fetchone()
        return str(row["id"])


def _finish_index_run(run_id: str, stats: IndexStats) -> None:
    with _connect() as conn:
        conn.execute(
            """
            update gpc_index_runs
            set
                status = 'succeeded',
                finished_at = now(),
                files_seen = %s,
                files_indexed = %s,
                chunks_written = %s,
                metadata = metadata || %s
            where id = %s
            """,
            (
                stats.files_seen,
                stats.files_indexed,
                stats.chunks_written,
                Jsonb(
                    {
                        "discovery_mode": stats.discovery_mode,
                        "files_discovered": stats.files_discovered,
                        "files_unchanged": stats.files_unchanged,
                        "files_skipped": stats.files_skipped,
                        "files_failed": stats.files_failed,
                        "chunks_deleted": stats.chunks_deleted,
                        "points_deleted": stats.points_deleted,
                        "skipped_reasons": stats.skipped_reasons,
                        "errors": stats.errors,
                    }
                ),
                run_id,
            ),
        )


def _fail_index_run(run_id: str, exc: Exception) -> None:
    with _connect() as conn:
        conn.execute(
            """
            update gpc_index_runs
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
                run_id,
            ),
        )


def _options_metadata(options: IndexOptions) -> dict[str, Any]:
    return {
        "reset": options.reset,
        "force": options.force,
        "prune_deleted": options.prune_deleted,
        "fail_fast": options.fail_fast,
        "include_unknown_text": options.include_unknown_text,
        "follow_symlinks": options.follow_symlinks,
        "max_file_bytes": options.max_file_bytes,
        "chunk_chars": options.chunk_chars,
        "batch_size": options.batch_size,
        "limit_files": options.limit_files,
    }


def _connect() -> psycopg.Connection:
    return psycopg.connect(POSTGRES_DSN, row_factory=dict_row)


def _language_for_path(path: Path) -> str | None:
    if path.name == "Dockerfile":
        return "dockerfile"
    if path.name == "Makefile":
        return "makefile"
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower())


def _file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CODE_EXTENSIONS or path.name in {"Dockerfile", "Makefile"}:
        return "code"
    if suffix in DOC_EXTENSIONS or path.name == "AGENTS.md":
        return "doc"
    if suffix in CONFIG_EXTENSIONS or path.name in {
        ".dockerignore",
        ".env.example",
        ".env.sample",
        ".env.template",
        ".gitignore",
        ".gpc.yaml",
    }:
        return "config"
    return "text"


def _chunk_type(path: Path) -> str:
    file_type = _file_type(path)
    if file_type == "doc":
        return "documentation"
    return file_type


def _is_supported_text_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS or path.name in SUPPORTED_FILENAMES


def _looks_sensitive_filename(filename: str) -> bool:
    if filename in SAFE_ENV_EXAMPLES:
        return False
    if filename in SENSITIVE_FILENAMES:
        return True
    if filename.startswith(".env."):
        return True
    if Path(filename).suffix.lower() in SENSITIVE_EXTENSIONS:
        return True
    return any(fnmatch.fnmatch(filename, pattern) for pattern in SENSITIVE_NAME_PATTERNS)


def _looks_sensitive_content(text: str) -> bool:
    sample = text[:200_000]
    return any(pattern.search(sample) for pattern in SENSITIVE_CONTENT_PATTERNS)


def _should_ignore_dir(rel_path: Path, dirname: str, gitignore_patterns: list[str]) -> bool:
    if dirname in IGNORED_DIRS:
        return True
    return _matches_gitignore(rel_path.as_posix(), is_dir=True, patterns=gitignore_patterns)


def _load_gitignore_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        return []

    patterns = []
    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _matches_gitignore(relative_path: str, *, is_dir: bool, patterns: list[str]) -> bool:
    normalized = relative_path.strip("/")
    parts = normalized.split("/")

    for pattern in patterns:
        pattern = pattern.strip()
        directory_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if directory_only and not is_dir:
            continue

        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")

        if "/" in pattern or anchored:
            if fnmatch.fnmatch(normalized, pattern):
                return True
            continue

        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True

    return False
