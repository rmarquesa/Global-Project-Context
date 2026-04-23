from __future__ import annotations

import argparse
from pathlib import Path

from gpc.status import get_index_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show GPC index status for a project.")
    parser.add_argument("--project", help="Project slug or alias.")
    parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="Working directory used for project resolution when --project is omitted.",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of recent runs to show.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    status = get_index_status(project=args.project, cwd=args.cwd, runs=args.runs)
    project = status["project"]

    print(
        f"project={project['slug']} files={status['files']} chunks={status['chunks']} "
        f"qdrant_points={status['qdrant_points']}"
    )
    for run in status["runs"]:
        metadata = run["metadata"] or {}
        print(
            "run "
            f"status={run['status']} files_seen={run['files_seen']} "
            f"files_indexed={run['files_indexed']} "
            f"files_unchanged={metadata.get('files_unchanged', 0)} "
            f"files_failed={metadata.get('files_failed', 0)} "
            f"chunks_written={run['chunks_written']} "
            f"started_at={run['started_at']}"
        )


if __name__ == "__main__":
    main()
