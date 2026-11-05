"""Content-free candidate capture v1 and bounded, retrieval-free replay.

Only authoritative hydrated rows supply identities. Channel ranks count unique
IDs (first occurrence wins), matching retrieval; backend_rank retains the first
raw position. hydrated/dropped count unique IDs; raw_returned includes duplicates.
Dropped IDs and their metadata are never serialized. Captures are copied JSON
values, not references to backend rows or SearchResults.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any


def capture_sha256(capture: dict[str, Any]) -> str:
    """Fingerprint this safe capture only, not corpus or model state."""
    return hashlib.sha256(
        json.dumps(
            capture, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def build_capture(
    channels: dict[str, list[tuple[str, float]]],
    ranked: list,
    *,
    mode: str,
    task: str,
    k: int,
    candidate_limit: int,
) -> dict[str, Any]:
    hydrated = {
        result.chunk_id: {
            "chunk_id": result.chunk_id,
            "repo_slug": result.repo_slug,
            "relative_path": result.relative_path,
            "file_identity": (
                f"{result.repo_slug}:{result.relative_path}"
                if result.repo_slug
                else result.relative_path
            ),
        }
        for _, result in ranked
    }
    captured_channels = {}
    for channel, candidates in channels.items():
        seen: set[str] = set()
        entries = []
        for position, (chunk_id, score) in enumerate(candidates, 1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            if chunk_id in hydrated:
                entries.append(
                    {
                        **hydrated[chunk_id],
                        "rank": len(seen),
                        "backend_rank": position,
                        "score": float(score),
                    }
                )
        captured_channels[channel] = {
            "raw_returned": len(candidates),
            "unique_returned": len(seen),
            "duplicates_returned": len(candidates) - len(seen),
            "hydrated": len(entries),
            "dropped": len(seen) - len(entries),
            "candidates": entries,
        }
    capture = {
        "version": 1,
        "mode": mode,
        "task": task,
        "k": k,
        "candidate_limit": candidate_limit,
        "channels": captured_channels,
        "ranked_candidates": [
            {
                **hydrated[result.chunk_id],
                "rank": rank,
                "score": float(result.score),
                "ranking_score": float(ranking_score),
            }
            for rank, (ranking_score, result) in enumerate(ranked, 1)
        ],
    }
    # Validate all serialized numbers before publishing a successful capture.
    capture_sha256(capture)
    return capture


def replay_paths(capture: dict[str, Any], cap: int) -> list[str]:
    """Filter the existing ranked union; never refill or enlarge its budget."""
    if cap not in (1, 2):
        raise ValueError("candidate replay cap must be 1 or 2")
    counts: Counter[str] = Counter()
    paths = []
    for row in capture["ranked_candidates"]:
        identity = row["file_identity"]
        if counts[identity] < cap:
            paths.append(identity)
            counts[identity] += 1
        if len(paths) == capture["k"]:
            break
    return paths


def path_stats(paths: list[str], k: int) -> dict[str, Any]:
    counts = Counter(paths)
    return {
        "result_count": len(paths),
        "unfilled_slots": max(0, k - len(paths)),
        "short_results": len(paths) < k,
        "unique_files": len(counts),
        "repeated_paths": {path: count for path, count in counts.items() if count > 1},
    }


def lexical_ties(channel: dict[str, Any]) -> dict[str, Any]:
    """Scores describe hydrated lexical candidates only, never dropped rows."""
    distribution = Counter(row["score"] for row in channel.get("candidates", []))
    return {
        "scope": "hydrated lexical candidates",
        "distinct_scores": len(distribution),
        "largest_tie": max(distribution.values(), default=0),
        "score_distribution": [
            {"score": score, "count": count}
            for score, count in sorted(distribution.items(), reverse=True)
        ],
    }
