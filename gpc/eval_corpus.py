"""Read-only, fixture-bounded PostgreSQL availability evidence for evaluations.

This is not a vector-store audit or a check of current files on disk. Unknown
requested repositories fail explicitly in diagnostic mode instead of looking
like retrieval misses (ordinary search may simply return no candidates).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from gpc.config import POSTGRES_DSN
from gpc.registry import ProjectResolutionError, normalize_slug
from gpc.retrieval import query_terms
from gpc.search import _normalize_repos

if TYPE_CHECKING:
    from gpc.search_eval import SearchEvalCase


AVAILABILITY_STATUSES = (
    "indexed",
    "file_without_chunks",
    "not_indexed",
    "ambiguous",
    "out_of_scope",
)


@dataclass(frozen=True)
class EvalCorpusSnapshot:
    project_id: str
    project_slug: str
    repos: tuple[tuple[str, str], ...]  # (id, slug)
    files: tuple[tuple[str, str, str | None, bool], ...]  # slug, path, hash, chunks

    def to_dict(self, fixture_sha256: str) -> dict[str, Any]:
        metadata = {
            "project": {"id": self.project_id, "slug": self.project_slug},
            "repos": [{"id": id_, "slug": slug} for id_, slug in sorted(self.repos)],
            "files": sorted(self.files, key=lambda row: (row[0], row[1])),
        }
        digest = hashlib.sha256(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "project": metadata["project"],
            "repos": metadata["repos"],
            "matched_files": len(self.files),
            "metadata_sha256": digest,
            "fixture_sha256": fixture_sha256,
            "scope": "Fixture-label paths only, not a whole-corpus fingerprint.",
            "notes": [
                "Indexed means chunk-backed PostgreSQL metadata, not proof of Qdrant vector availability or fresh files on disk.",
                "One metadata snapshot is reused; retrieval is not atomic with it during concurrent index changes.",
                "No Git revision or index source revision was verified.",
            ],
        }

    def diagnose_case(
        self, case: SearchEvalCase, missing: list[str], case_index: int
    ) -> dict[str, Any]:
        scope = _normalize_repos(case.repo)
        labels = []
        for label in dict.fromkeys(case.expected_paths):
            qualifier, path = label.split(":", 1) if ":" in label else (None, label)
            matches = [
                row
                for row in self.files
                if row[1] == path
                and (qualifier is None or row[0] == qualifier)
                and (not scope or row[0] in scope)
            ]
            if qualifier is not None and scope and qualifier not in scope:
                status = "out_of_scope"
            elif qualifier is None and len({row[0] for row in matches}) > 1:
                status = "ambiguous"
            elif any(row[3] for row in matches):
                status = "indexed"
            elif matches:
                status = "file_without_chunks"
            else:
                status = "not_indexed"
            labels.append(
                {
                    "path": label,
                    "status": status,
                    "matching_paths": sorted({f"{row[0]}:{row[1]}" for row in matches}),
                }
            )
        return {
            "case_index": case_index,
            "expected_paths": labels,
            "coverage": {
                status: sum(row["status"] == status for row in labels)
                for status in AVAILABILITY_STATUSES
            },
            "indexed_but_not_retrieved_paths": [
                row["path"]
                for row in labels
                if row["status"] == "indexed" and row["path"] in missing
            ],
            "not_indexed_expected_paths": [
                row["path"]
                for row in labels
                if row["status"] in ("not_indexed", "file_without_chunks")
            ],
            "ambiguous_expected_paths": [
                row["path"] for row in labels if row["status"] == "ambiguous"
            ],
            "out_of_scope_expected_paths": [
                row["path"] for row in labels if row["status"] == "out_of_scope"
            ],
        }


def load_eval_corpus(project: str, cases: list[SearchEvalCase]) -> EvalCorpusSnapshot:
    """Resolve explicit project/alias like search, then read bounded metadata.

    A dedicated connection keeps transaction settings away from pooled clients.
    All names and label paths are parameters (never SQL patterns).
    """
    slug = normalize_slug(project)
    for case in cases:
        query_terms(case.query)
    scopes = [_normalize_repos(case.repo) for case in cases]
    paths = sorted(
        {label.split(":", 1)[-1] for c in cases for label in c.expected_paths}
    )
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("set transaction isolation level repeatable read, read only")
        conn.execute("set local statement_timeout = '5s'")
        resolved = conn.execute(
            """
            select p.id::text as id, p.slug
            from gpc_projects p
            where p.slug = %s or exists (
                select 1 from gpc_project_aliases a
                where a.project_id = p.id and a.alias = %s
            )
            order by case when p.slug = %s then 0 else 1 end
            limit 1
            """,
            (slug, slug, slug),
        ).fetchone()
        if resolved is None:
            raise ProjectResolutionError(f"Unknown project or alias: {project!r}")
        repos = conn.execute(
            "select id::text as id, slug from gpc_repos where project_id = %s::uuid",
            (resolved["id"],),
        ).fetchall()
        known = {row["slug"] for row in repos}
        unknown = sorted({r for scope in scopes if scope for r in scope} - known)
        if unknown:
            raise ValueError(f"Unknown repositories in evaluation project: {unknown!r}")
        files = []
        if paths:
            files = conn.execute(
                """
                select r.slug as repo_slug, f.relative_path, f.content_hash,
                    exists (
                        select 1 from gpc_chunks c
                        where c.file_id = f.id and c.project_id = f.project_id
                          and coalesce(c.repo_id, f.repo_id) = r.id
                          and c.source_type = 'project_file'
                    ) as has_chunks
                from gpc_files f
                join gpc_repos r on r.project_id = f.project_id
                  and (r.id = f.repo_id or (f.repo_id is null and exists (
                      select 1 from gpc_chunks c
                      where c.file_id = f.id and c.project_id = f.project_id
                        and c.repo_id = r.id and c.source_type = 'project_file'
                  )))
                where f.project_id = %s::uuid and f.relative_path = any(%s::text[])
                """,
                (resolved["id"], paths),
            ).fetchall()
    return EvalCorpusSnapshot(
        resolved["id"],
        resolved["slug"],
        tuple(sorted((row["id"], row["slug"]) for row in repos)),
        tuple(
            sorted(
                {
                    (
                        row["repo_slug"],
                        row["relative_path"],
                        row["content_hash"],
                        row["has_chunks"],
                    )
                    for row in files
                },
                key=lambda row: (row[0], row[1]),
            )
        ),
    )
