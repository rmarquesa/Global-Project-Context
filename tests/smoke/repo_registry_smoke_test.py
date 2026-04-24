"""Smoke test for the project+repo registry model.

Creates an isolated project + two repos in Postgres, verifies cwd-based
resolution, and exercises ``consolidate_projects``. Always cleans up, even on
failure.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile

from gpc.registry import (
    connect,
    consolidate_projects,
    ensure_project,
    list_projects,
    list_repos,
    register_project,
    register_repo,
    resolve_repo,
)


PROJECT_SLUG = "gpc_smoke_repo"
REPO_A_SLUG = "smoke_repo_a"
REPO_B_SLUG = "smoke_repo_b"
CONSOLIDATE_TARGET = "gpc_smoke_consolidate"
CONSOLIDATE_SOURCE_A = "gpc_smoke_consolidate_src_a"
CONSOLIDATE_SOURCE_B = "gpc_smoke_consolidate_src_b"


def _cleanup(slugs: list[str]) -> None:
    with connect() as conn:
        for slug in slugs:
            conn.execute(
                "delete from gpc_projects where slug = %s",
                (slug,),
            )


@contextmanager
def _tmpdir(name: str):
    tmp = tempfile.mkdtemp(prefix=f"gpc-smoke-{name}-")
    try:
        yield Path(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    slugs = [
        PROJECT_SLUG,
        CONSOLIDATE_TARGET,
        CONSOLIDATE_SOURCE_A,
        CONSOLIDATE_SOURCE_B,
    ]
    _cleanup(slugs)
    try:
        with _tmpdir("a") as repo_a, _tmpdir("b") as repo_b:
            # Scenario 1 — create a logical project and attach two repos.
            project = ensure_project(
                slug=PROJECT_SLUG,
                name="Smoke Repo Project",
                aliases=["smoke_repo_alias"],
            )
            assert project["slug"] == PROJECT_SLUG, project
            assert "smoke_repo_alias" in project.get("aliases", []), project

            repo_a_row = register_repo(
                PROJECT_SLUG,
                repo_a,
                slug=REPO_A_SLUG,
                description="first smoke repo",
            )
            repo_b_row = register_repo(
                "smoke_repo_alias",  # resolve by alias
                repo_b,
                slug=REPO_B_SLUG,
            )
            assert repo_a_row["project_slug"] == PROJECT_SLUG, repo_a_row
            assert repo_b_row["project_slug"] == PROJECT_SLUG, repo_b_row

            listed = [r["slug"] for r in list_repos(PROJECT_SLUG)]
            assert set(listed) >= {REPO_A_SLUG, REPO_B_SLUG}, listed

            # cwd-based resolution picks the right repo.
            resolved = resolve_repo(cwd=str(repo_a / "nested"))
            assert resolved["project"]["slug"] == PROJECT_SLUG, resolved
            assert resolved["repo"]["slug"] == REPO_A_SLUG, resolved

            resolved_explicit = resolve_repo(
                project=PROJECT_SLUG, repo=REPO_B_SLUG
            )
            assert resolved_explicit["repo"]["slug"] == REPO_B_SLUG

        # Scenario 2 — consolidate two standalone projects under a target.
        with _tmpdir("src-a") as src_a, _tmpdir("src-b") as src_b:
            register_project(src_a, slug=CONSOLIDATE_SOURCE_A, name="src a")
            register_project(src_b, slug=CONSOLIDATE_SOURCE_B, name="src b")

            stats = consolidate_projects(
                CONSOLIDATE_TARGET,
                [CONSOLIDATE_SOURCE_A, CONSOLIDATE_SOURCE_B],
                target_name="Consolidated",
                delete_source_projects=True,
            )
            assert stats["repos_created"] == 2, stats
            assert stats["source_projects_deleted"] == 2, stats

            target_projects = [p["slug"] for p in list_projects()]
            assert CONSOLIDATE_TARGET in target_projects
            assert CONSOLIDATE_SOURCE_A not in target_projects
            assert CONSOLIDATE_SOURCE_B not in target_projects

            target_repos = [r["slug"] for r in list_repos(CONSOLIDATE_TARGET)]
            assert set(target_repos) == {CONSOLIDATE_SOURCE_A, CONSOLIDATE_SOURCE_B}, target_repos

    finally:
        _cleanup(slugs)

    print("repo_registry_smoke_test=passed")


if __name__ == "__main__":
    main()
