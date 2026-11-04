from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager
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
from qdrant_client.models import (
    Filter,
    FieldCondition,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
    PointStruct,
)

from gpc.db import get_qdrant, pg_connection
from gpc.config import COLLECTION_NAME
from gpc.embeddings import embed_texts
from gpc.index_lock import repository_lock
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
SUPPORTED_EXTENSIONS = (
    CODE_EXTENSIONS | DOC_EXTENSIONS | CONFIG_EXTENSIONS | {".css", ".html", ".sql"}
)
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
    with repository_lock(root_path):
        return _index_project_path(
            root_path,
            slug=slug,
            name=name,
            description=description,
            options=options,
            repo_slug=repo_slug,
        )


def _index_project_path(
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

    qdrant = get_qdrant()
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
            files_discovered=len(discovery.candidates)
            + sum(discovery.skipped.values()),
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
    root = root.resolve(strict=True)
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
        removed = _remove_file_index(project, candidate.relative_path, qdrant)
        return FileIndexResult(
            status="skipped",
            reason="binary_content",
            chunks_deleted=removed.chunks_deleted,
            points_deleted=removed.points_deleted,
        )

    text = raw.decode("utf-8", errors="replace")
    if _looks_sensitive_content(text):
        removed = _remove_file_index(project, candidate.relative_path, qdrant)
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
              and repo_id is not distinct from %s
            """,
            (project["id"], candidate.relative_path, project.get("_repo_id")),
        ).fetchone()
        if (
            not options.force
            and existing
            and existing["content_hash"] == file_hash
            and existing["indexed_at"] is not None
        ):
            rows = conn.execute(
                "select qdrant_point_id from gpc_chunks where file_id = %s",
                (existing["id"],),
            ).fetchall()
            expected_ids = {row["qdrant_point_id"] for row in rows}
            # Postgres completion alone cannot detect a recreated collection or
            # lost points. Also check for leftovers from an interrupted update.
            if expected_ids and expected_ids == _file_point_ids(
                project, existing["id"], qdrant
            ):
                return FileIndexResult(status="unchanged")

    chunks = chunk_text(
        text,
        relative_path=candidate.relative_path,
        chunk_type=_chunk_type(candidate.absolute_path),
        max_chars=options.chunk_chars,
    )
    if not chunks:
        removed = _remove_file_index(project, candidate.relative_path, qdrant)
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
                    "repo_id": (
                        str(project["_repo_id"]) if project.get("_repo_id") else None
                    ),
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

    qdrant.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
    # Query the actual file projection, not only the previous SQL chunk IDs:
    # SQL replacement may already have committed during an earlier failed try.
    stale_point_ids = sorted(
        _file_point_ids(project, file_row["id"], qdrant) - set(new_point_ids)
    )
    if stale_point_ids:
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=stale_point_ids,
            wait=True,
        )

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
                _append_chunk(
                    chunks,
                    relative_path,
                    chunk_type,
                    [paragraph[start : start + max_chars]],
                )
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
        # Git lists a tracked directory symlink, not the files below it. Only
        # traverse it after the same containment/ignore checks used by os.walk.
        if (
            options.follow_symlinks
            and absolute_path.is_symlink()
            and absolute_path.is_dir()
        ):
            resolved = _resolve_discovery_path(root, absolute_path, options, skipped)
            if resolved is None:
                continue
            if _ignored_discovery_path(root, absolute_path, resolved, is_dir=True):
                skipped["ignored_dir"] += 1
                continue
            linked = _walk_discover_files(root, options, start=absolute_path)
            candidates.extend(linked.candidates)
            skipped.update(linked.skipped)
            if options.limit_files and len(candidates) >= options.limit_files:
                candidates = candidates[: options.limit_files]
                break
            continue
        candidate = _candidate_from_path(root, absolute_path, options, skipped)
        if not candidate:
            continue

        candidates.append(candidate)
        if options.limit_files and len(candidates) >= options.limit_files:
            break

    return DiscoveryResult(candidates=candidates, skipped=skipped, mode=mode)


def _walk_discover_files(
    root: Path, options: IndexOptions, *, start: Path | None = None
) -> DiscoveryResult:
    root = root.resolve(strict=True)
    skipped: Counter[str] = Counter()
    candidates: list[FileCandidate] = []
    gitignore_patterns = _load_gitignore_patterns(root)

    for current_root, dirnames, filenames in os.walk(
        start or root, followlinks=options.follow_symlinks
    ):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(root)
        kept_dirs = []
        for dirname in sorted(dirnames):
            rel_path = rel_dir / dirname
            if _should_ignore_dir(rel_path, dirname, gitignore_patterns):
                skipped["ignored_dir"] += 1
                continue
            absolute_dir = current_path / dirname
            resolved = _resolve_discovery_path(root, absolute_dir, options, skipped)
            if resolved is None:
                continue
            if _ignored_discovery_path(root, absolute_dir, resolved, is_dir=True):
                skipped["ignored_dir"] += 1
                continue
            # Reject directory-link cycles without suppressing legitimate aliases
            # of an in-root directory reached along a different lexical path.
            ancestors = [current_path, *current_path.parents]
            if resolved in {
                ancestor.resolve()
                for ancestor in ancestors
                if ancestor.is_relative_to(root)
            }:
                skipped["symlink_cycle"] += 1
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
                return DiscoveryResult(
                    candidates=candidates, skipped=skipped, mode="walk"
                )

    return DiscoveryResult(candidates=candidates, skipped=skipped, mode="walk")


def _candidate_from_path(
    root: Path,
    absolute_path: Path,
    options: IndexOptions,
    skipped: Counter[str],
) -> FileCandidate | None:
    root = root.resolve(strict=True)
    absolute_path = Path(os.path.abspath(absolute_path))
    resolved = _resolve_discovery_path(root, absolute_path, options, skipped)
    if resolved is None:
        return None
    try:
        if not resolved.is_file():
            skipped["not_file"] += 1
            return None
        stat = resolved.stat()
    except OSError:
        skipped["stat_error"] += 1
        return None

    relative_path = absolute_path.relative_to(root).as_posix()
    if _ignored_discovery_path(root, absolute_path, resolved, is_dir=False):
        skipped["ignored_path"] += 1
        return None
    # An innocuous alias must not bypass the target's filename policy.
    for path in {absolute_path, resolved}:
        if path.name in IGNORED_FILES:
            skipped["ignored_file"] += 1
            return None
        if _looks_sensitive_filename(path.name):
            skipped["sensitive_filename"] += 1
            return None
        if path.suffix.lower() in BINARY_EXTENSIONS:
            skipped["binary_extension"] += 1
            return None
        if not options.include_unknown_text and not _is_supported_text_path(path):
            skipped["unsupported_extension"] += 1
            return None
    if stat.st_size > options.max_file_bytes:
        skipped["too_large"] += 1
        return None

    return FileCandidate(
        absolute_path=absolute_path,
        relative_path=relative_path,
        size_bytes=stat.st_size,
        language=_language_for_path(absolute_path),
        file_type=_file_type(absolute_path),
    )


def _resolve_discovery_path(
    root: Path, path: Path, options: IndexOptions, skipped: Counter[str]
) -> Path | None:
    """Check lexical identity before allowing any symlink target into discovery."""
    try:
        relative = path.relative_to(root)
        if ".." in relative.parts:
            raise ValueError("parent traversal")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not options.follow_symlinks and any(
            (root / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        ):
            skipped["symlink"] += 1
            return None
        return resolved
    except ValueError:
        skipped["outside_root"] += 1
    except (OSError, RuntimeError):
        skipped["stat_error"] += 1
    return None


def _ignored_discovery_path(
    root: Path, path: Path, resolved: Path, *, is_dir: bool
) -> bool:
    patterns = _load_gitignore_patterns(root)
    for candidate in {path, resolved}:
        relative = candidate.relative_to(root)
        directories = relative.parts if is_dir else relative.parts[:-1]
        if any(dirname in IGNORED_DIRS for dirname in directories):
            return True

    ignored_candidates = {path, resolved}
    if path != resolved:
        # Git knows nested ignore files and directory rules that the lightweight
        # non-Git walker does not. --no-index also protects ignored tracked targets.
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "check-ignore",
                    "--no-index",
                    "-q",
                    "--",
                    resolved.relative_to(root).as_posix(),
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return True  # Cannot establish the target's ignore policy safely.
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            # Git's result includes nested rules and negations. Do not override
            # an explicitly allowed target with the walker's simpler matcher.
            ignored_candidates.discard(resolved)
        elif _is_git_worktree(root):
            return True

    for candidate in ignored_candidates:
        relative = candidate.relative_to(root)
        directories = relative.parts if is_dir else relative.parts[:-1]
        if any(
            _should_ignore_dir(Path(*directories[:i]), dirname, patterns)
            for i, dirname in enumerate(directories, start=1)
        ) or _matches_gitignore(relative.as_posix(), is_dir=is_dir, patterns=patterns):
            return True
    return False


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
        # Preserve the lexical path: resolving here erases symlink identity and
        # lets aliases bypass follow_symlinks=False and target ignore checks.
        absolute_path = repo_root / raw_path
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


def _clear_project_index(
    project: dict[str, Any], qdrant: QdrantClient
) -> FileIndexResult:
    qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=_projection_filter(project),
        wait=True,
    )
    with _connect() as conn:
        conn.execute(
            """
            delete from gpc_files
            where project_id = %s and repo_id is not distinct from %s
            """,
            (project["id"], project.get("_repo_id")),
        )
    return FileIndexResult(status="indexed", chunks_deleted=0, points_deleted=0)


def _projection_filter(project: dict[str, Any], file_id: Any = None) -> Filter:
    conditions: list[Any] = [
        FieldCondition(key="project_id", match=MatchValue(value=str(project["id"])))
    ]
    repo_id = project.get("_repo_id")
    conditions.append(
        FieldCondition(key="repo_id", match=MatchValue(value=str(repo_id)))
        if repo_id is not None
        else IsEmptyCondition(is_empty=PayloadField(key="repo_id"))
    )
    if file_id is not None:
        conditions.append(
            FieldCondition(key="file_id", match=MatchValue(value=str(file_id)))
        )
    return Filter(must=conditions)


def _file_point_ids(
    project: dict[str, Any], file_id: Any, qdrant: QdrantClient
) -> set[str]:
    point_ids: set[str] = set()
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=_projection_filter(project, file_id),
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids.update(str(point.id) for point in points)
        if offset is None:
            return point_ids


def _remove_file_index(
    project: dict[str, Any],
    relative_path: str,
    qdrant: QdrantClient,
) -> FileIndexResult:
    with _connect() as conn:
        rows = conn.execute(
            """
            select f.id, c.qdrant_point_id
            from gpc_files f
            left join gpc_chunks c on c.file_id = f.id
            where f.project_id = %s and f.relative_path = %s
              and f.repo_id is not distinct from %s
            """,
            (project["id"], relative_path, project.get("_repo_id")),
        ).fetchall()
    point_ids = set()
    for file_id in {row["id"] for row in rows}:
        point_ids.update(_file_point_ids(project, file_id, qdrant))

    if point_ids:
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=sorted(point_ids),
            wait=True,
        )

    with _connect() as conn:
        conn.execute(
            """
            delete from gpc_files
            where project_id = %s and relative_path = %s
              and repo_id is not distinct from %s
            """,
            (project["id"], relative_path, project.get("_repo_id")),
        )

    return FileIndexResult(
        status="indexed",
        chunks_deleted=sum(row["qdrant_point_id"] is not None for row in rows),
        points_deleted=len(point_ids),
    )


def _prune_missing_files(
    project: dict[str, Any],
    discovered_paths: set[str],
    qdrant: QdrantClient,
) -> FileIndexResult:
    discovered = sorted(discovered_paths)
    with _connect() as conn:
        rows = conn.execute(
            """
            select relative_path from gpc_files
            where project_id = %s and repo_id is not distinct from %s
              and not (relative_path = any(%s::text[]))
            """,
            (project["id"], project.get("_repo_id"), discovered),
        ).fetchall()

    chunks_deleted = 0
    points_deleted = 0
    for row in rows:
        removed = _remove_file_index(project, row["relative_path"], qdrant)
        chunks_deleted += removed.chunks_deleted
        points_deleted += removed.points_deleted

    return FileIndexResult(
        status="indexed" if rows else "unchanged",
        chunks_deleted=chunks_deleted,
        points_deleted=points_deleted,
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


def _connect() -> AbstractContextManager[psycopg.Connection]:
    return pg_connection(row_factory=dict_row)


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
    return (
        path.suffix.lower() in SUPPORTED_EXTENSIONS or path.name in SUPPORTED_FILENAMES
    )


def _looks_sensitive_filename(filename: str) -> bool:
    if filename in SAFE_ENV_EXAMPLES:
        return False
    if filename in SENSITIVE_FILENAMES:
        return True
    if filename.startswith(".env."):
        return True
    if Path(filename).suffix.lower() in SENSITIVE_EXTENSIONS:
        return True
    return any(
        fnmatch.fnmatch(filename, pattern) for pattern in SENSITIVE_NAME_PATTERNS
    )


def _looks_sensitive_content(text: str) -> bool:
    # Discovery bounds accepted files; scan all accepted content, not a prefix.
    return any(pattern.search(text) for pattern in SENSITIVE_CONTENT_PATTERNS)


def _should_ignore_dir(
    rel_path: Path, dirname: str, gitignore_patterns: list[str]
) -> bool:
    if dirname in IGNORED_DIRS:
        return True
    return _matches_gitignore(
        rel_path.as_posix(), is_dir=True, patterns=gitignore_patterns
    )


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


def _matches_gitignore(
    relative_path: str, *, is_dir: bool, patterns: list[str]
) -> bool:
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
