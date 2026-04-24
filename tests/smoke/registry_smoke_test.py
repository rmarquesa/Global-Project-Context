from pathlib import Path

from gpc.registry import (
    link_project_source,
    register_project,
    register_source,
    resolve_project,
)


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    project = register_project(
        ROOT,
        slug="gpc",
        name="GPC",
        aliases=["global-project-context"],
        description="Global Project Context infrastructure",
        primary_language="python",
    )
    print(f"project={project['slug']} aliases={','.join(project['aliases'])}")

    root_source = register_source(
        ROOT,
        slug="gpc-root",
        name="GPC Root",
        source_type="project_root",
    )
    link_project_source("gpc", root_source["slug"], role="primary")
    print(f"source={root_source['slug']} type={root_source['source_type']}")

    graphify_path = ROOT / "graphify-out"
    if graphify_path.exists():
        graphify_source = register_source(
            graphify_path,
            slug="gpc-graphify",
            name="GPC Graphify Output",
            source_type="graphify_output",
        )
        link_project_source("gpc", graphify_source["slug"], role="graph")
        print(f"source={graphify_source['slug']} type={graphify_source['source_type']}")

    by_cwd = resolve_project(cwd=ROOT / "migrations")
    print(f"resolve_cwd={by_cwd['slug']} reason={by_cwd['resolution_reason']}")

    by_alias = resolve_project(project="global-project-context")
    print(f"resolve_alias={by_alias['slug']} reason={by_alias['resolution_reason']}")

    # Regression guard: when both `project` and `cwd` are provided and point
    # at different registered projects, the explicit slug must win. This
    # matches the priority order documented in docs/architecture.md and
    # brings resolve_project in line with resolve_repo.
    by_explicit = resolve_project(project="gpc", cwd="/tmp")
    assert by_explicit["slug"] == "gpc", by_explicit
    assert by_explicit.get("resolution_input") == "project", by_explicit
    print(
        f"resolve_explicit_wins={by_explicit['slug']} "
        f"input={by_explicit.get('resolution_input')}"
    )


if __name__ == "__main__":
    main()
