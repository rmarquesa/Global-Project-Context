from __future__ import annotations

import argparse
from pathlib import Path

from gpc.search import search_project_context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search indexed GPC project context.")
    parser.add_argument("query", help="Semantic search query.")
    parser.add_argument("--project", help="Project slug or alias.")
    parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="Working directory used for project resolution when --project is omitted.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Number of results to return.")
    parser.add_argument(
        "--content-chars",
        type=int,
        default=700,
        help="Maximum content characters to print per result.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project, results = search_project_context(
        args.query,
        project=args.project,
        cwd=args.cwd,
        limit=args.limit,
    )

    print(f"project={project['slug']} results={len(results)}")
    for index, result in enumerate(results, start=1):
        content = result.content.strip().replace("\n\n\n", "\n\n")
        if len(content) > args.content_chars:
            content = f"{content[:args.content_chars].rstrip()}..."

        print(
            f"\n[{index}] score={result.score:.4f} "
            f"path={result.relative_path} chunk={result.chunk_id}"
        )
        print(content)


if __name__ == "__main__":
    main()
