from __future__ import annotations

import argparse
from pathlib import Path

from gpc.indexer import IndexOptions, index_project_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index a local project into GPC.")
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project root path to index.",
    )
    parser.add_argument("--slug", help="Project slug. Defaults to the directory name.")
    parser.add_argument("--name", help="Project display name. Defaults to the directory name.")
    parser.add_argument("--description", help="Optional project description.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing indexed files/chunks for this project before indexing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed files even when their content hash is unchanged.",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Do not remove indexed files that no longer exist or are no longer discoverable.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first file indexing error.",
    )
    parser.add_argument(
        "--include-unknown-text",
        action="store_true",
        help="Index unknown text-like files instead of only known code/doc/config paths.",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinked files/directories. Disabled by default.",
    )
    parser.add_argument(
        "--limit-files",
        type=int,
        help="Index only the first N discovered files. Useful for smoke tests.",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=512_000,
        help="Skip files larger than this size. Default: 512000.",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=3_500,
        help="Approximate max characters per chunk. Default: 3500.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="Number of chunks to embed per Ollama request. Default: 16.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = index_project_path(
        Path(args.path),
        slug=args.slug,
        name=args.name,
        description=args.description,
        options=IndexOptions(
            reset=args.reset,
            force=args.force,
            prune_deleted=not args.no_prune,
            fail_fast=args.fail_fast,
            include_unknown_text=args.include_unknown_text,
            follow_symlinks=args.follow_symlinks,
            max_file_bytes=args.max_file_bytes,
            chunk_chars=args.chunk_chars,
            batch_size=args.batch_size,
            limit_files=args.limit_files,
        ),
    )
    print(
        f"project={stats.project_slug} mode={stats.discovery_mode} "
        f"files_seen={stats.files_seen} files_indexed={stats.files_indexed} "
        f"files_unchanged={stats.files_unchanged} files_skipped={stats.files_skipped} "
        f"files_failed={stats.files_failed} chunks_written={stats.chunks_written} "
        f"chunks_deleted={stats.chunks_deleted} points_deleted={stats.points_deleted}"
    )
    if stats.skipped_reasons:
        reasons = ", ".join(
            f"{reason}:{count}" for reason, count in sorted(stats.skipped_reasons.items())
        )
        print(f"skipped_reasons={reasons}")
    if stats.errors:
        print("errors:")
        for error in stats.errors:
            print(f"- {error}")


if __name__ == "__main__":
    main()
