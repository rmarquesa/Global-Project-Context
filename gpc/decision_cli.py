"""Explicit decision-memory CLI; no writes occur during parser construction."""

from __future__ import annotations

import argparse
import json
import sys

import psycopg
from psycopg.errors import UndefinedColumn, UndefinedTable

from gpc import decisions


def register_decision_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "decision", help="Explicitly curate or inspect indexed decision memory."
    )
    actions = parser.add_subparsers(dest="decision_action", required=True)
    for action in ("capture", "approve", "invalidate", "show", "list"):
        child = actions.add_parser(action)
        child.add_argument("--project", required=True, help="Project slug or alias.")
        writing = action in {"capture", "approve", "invalidate"}
        child.add_argument("--repo", required=writing, help="Registered repo slug.")
        child.add_argument("--json", action="store_true", help="Emit JSON.")
        if writing:
            child.add_argument(
                "--actor", required=True, help="Local attribution, not authentication."
            )
        if action in {"approve", "invalidate", "show"}:
            child.add_argument("decision_id")
        if action == "capture":
            child.add_argument("--key", dest="decision_key", required=True)
            child.add_argument("--title", required=True)
            child.add_argument("--content", required=True)
            child.add_argument("--source-path", required=True)
            child.add_argument(
                "--source-revision",
                help="Caller-provided label; not a verified commit.",
            )
        elif action in {"approve", "invalidate"}:
            child.add_argument("--reason", required=action == "invalidate")
            if action == "approve":
                child.add_argument(
                    "--supersede", help="Approved decision ID to supersede."
                )
        elif action == "list":
            child.add_argument("--path")
            child.add_argument("--query")
            child.add_argument("--limit", type=int, default=20)
            child.add_argument("--include-inactive", action="store_true")
        child.set_defaults(func=cmd_decision)


def cmd_decision(args: argparse.Namespace) -> int:
    scope = {"project": args.project, "repo": args.repo}
    action = args.decision_action
    try:
        if action == "capture":
            result = decisions.capture_decision(
                **scope,
                decision_key=args.decision_key,
                title=args.title,
                content=args.content,
                source_path=args.source_path,
                actor=args.actor,
                source_revision=args.source_revision,
            )
        elif action == "approve":
            result = decisions.approve_decision(
                args.decision_id,
                **scope,
                actor=args.actor,
                reason=args.reason,
                supersede=args.supersede,
            )
        elif action == "invalidate":
            result = decisions.invalidate_decision(
                args.decision_id, **scope, actor=args.actor, reason=args.reason
            )
        elif action == "show":
            result = decisions.get_decision(args.decision_id, **scope)
        else:
            result = decisions.list_decisions(
                **scope,
                path=args.path,
                query=args.query,
                limit=args.limit,
                include_inactive=args.include_inactive,
            )
    except decisions.DecisionError as exc:
        print(f"gpc decision: {exc}", file=sys.stderr)
        return 1
    except (UndefinedTable, UndefinedColumn):
        print(
            "gpc decision: Decision schema unavailable; apply migration 0011 "
            "using the documented migration procedure.",
            file=sys.stderr,
        )
        return 1
    except psycopg.Error:
        print("gpc decision: Decision database operation failed.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            "Evidence: indexed content hash, not live files. Actor: local attribution."
        )
        if action == "show" or (action == "list" and args.include_inactive):
            print("Diagnostic view; presence is not an endorsement.")
        for row in result if isinstance(result, list) else [result]:
            _print_decision(row)
        if action == "capture":
            print(
                "Pending review; explicit approval is required before default retrieval."
            )
    return 0


def _print_decision(row: dict) -> None:
    for key in (
        "id",
        "repo_slug",
        "decision_key",
        "title",
        "status",
        "freshness",
        "source_path",
        "source_hash",
        "source_file_id",
        "approved_by",
        "approved_at",
    ):
        print(f"{key}={row.get(key)}")
    if row.get("source_revision") is not None:
        print(f"source_revision (caller-provided, unverified)={row['source_revision']}")
    print(row.get("content", ""))
    if "events" in row:
        print("history=" + json.dumps(row["events"], ensure_ascii=False))
