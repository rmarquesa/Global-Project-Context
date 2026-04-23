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


if __name__ == "__main__":
    main()
