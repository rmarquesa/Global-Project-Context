from gpc.graph import neo4j_healthcheck, project_graph_to_neo4j


def main() -> None:
    print(f"neo4j={neo4j_healthcheck()}")
    stats = project_graph_to_neo4j()
    print(
        "projection="
        f"projects:{stats.projects_written},"
        f"entities:{stats.entities_written},"
        f"relations:{stats.relations_written}"
    )


if __name__ == "__main__":
    main()
