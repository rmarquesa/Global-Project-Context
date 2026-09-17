#!/usr/bin/env python3
"""Generate human-reviewed improvement proposals from GPC observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from gpc.auto_research import generate_proposal
from gpc.registry import list_projects


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate advisory proposals from GPC drift signals and self-metrics."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", help="Project slug to analyze.")
    scope.add_argument("--all", action="store_true", help="Analyze every registered project.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/proposals"),
        help="Directory for generated Markdown proposals.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace today's matching proposal.")
    parser.add_argument("--json", action="store_true", help="Print generated proposal metadata as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    projects = [args.project] if args.project else [str(project["slug"]) for project in list_projects()]
    results = []

    for project_slug in projects:
        try:
            result = generate_proposal(
                project_slug=project_slug,
                output_dir=args.output_dir,
                overwrite=args.overwrite,
            )
        except Exception as exc:
            print(f"project={project_slug} status=failed error={exc}", file=sys.stderr)
            return 1
        results.append(
            {
                "project_slug": result.project_slug,
                "path": str(result.path),
                "signal_count": result.signal_count,
                "snapshot_count": result.snapshot_count,
                "proposal_slug": result.proposal_slug,
            }
        )

    if args.json:
        print(json.dumps({"proposals": results}, indent=2))
    else:
        for result in results:
            print(
                f"project={result['project_slug']} proposal={result['path']} "
                f"signals={result['signal_count']} snapshots={result['snapshot_count']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
