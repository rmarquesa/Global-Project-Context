from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Iterable

from gpc.config import EMBEDDING_PROVIDER
from gpc.embeddings import active_embedding_model, embedding_dimension
from gpc.eval_corpus import AVAILABILITY_STATUSES, EvalCorpusSnapshot, load_eval_corpus
from gpc.eval_candidates import capture_sha256, lexical_ties, path_stats, replay_paths
from gpc.search import SEARCH_MODES, SEARCH_TASKS, search_project_context


def _validate_options(k: int, mode: str, task: str) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError("k must be a positive integer")
    if not isinstance(mode, str) or mode not in SEARCH_MODES:
        raise ValueError(f"invalid search evaluation mode: {mode!r}")
    if not isinstance(task, str) or task not in SEARCH_TASKS:
        raise ValueError(f"invalid search evaluation task: {task!r}")


@dataclass(frozen=True)
class SearchEvalCase:
    query: str
    expected_paths: list[str]
    repo: str | list[str] | None = None
    task: str | None = None


@dataclass(frozen=True)
class SearchEvalResult:
    query: str
    expected_paths: list[str]
    result_paths: list[str]
    hit_paths: list[str]
    missing_expected_paths: list[str]
    latency_ms: float = 0.0
    repo: str | list[str] | None = None
    task: str = "general"
    diagnostics: dict[str, Any] | None = None
    candidate_analysis: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.diagnostics is None:
            payload.pop("diagnostics")
        if self.candidate_analysis is None:
            payload.pop("candidate_analysis")
        return payload


@dataclass(frozen=True)
class SearchEvalReport:
    k: int
    provider: str
    model: str
    dimensions: int
    results: list[SearchEvalResult]
    mode: str = "semantic"
    task: str = "general"
    diagnostics: dict[str, Any] | None = None
    candidate_analysis: dict[str, Any] | None = None

    @property
    def summary(self) -> dict[str, Any]:
        expected = sum(len(r.expected_paths) for r in self.results)
        hits = sum(len(r.hit_paths) for r in self.results)
        reciprocal = []
        ndcgs = []
        for result in self.results:
            ranks = set(
                _label_ranks(
                    result.expected_paths, result.result_paths[: self.k]
                ).values()
            )
            reciprocal.append(1 / min(ranks) if ranks else 0.0)
            dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
            ideal = sum(
                1 / math.log2(i + 1)
                for i in range(1, min(len(result.expected_paths), self.k) + 1)
            )
            ndcgs.append(dcg / ideal if ideal else 0.0)
        latencies = sorted(r.latency_ms for r in self.results)
        p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else 0.0
        return {
            "queries": len(self.results),
            "expected_paths": expected,
            "hits": hits,
            "recall_at_k": hits / expected if expected else 0.0,
            "query_hit_rate": (
                sum(bool(r.hit_paths) for r in self.results) / len(self.results)
                if self.results
                else 0.0
            ),
            "mrr_at_k": sum(reciprocal) / len(reciprocal) if reciprocal else 0.0,
            "ndcg_at_k": sum(ndcgs) / len(ndcgs) if ndcgs else 0.0,
            "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "p95_latency_ms": p95,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "k": self.k,
            "mode": self.mode,
            "task": self.task,
            "latency_note": "Milliseconds are supplied or wall-clock measurements (zero if absent), not performance claims; p95 uses nearest rank.",
            "embedding": {
                "provider": self.provider,
                "model": self.model,
                "dimensions": self.dimensions,
            },
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }
        if self.diagnostics is not None:
            payload["diagnostics"] = self.diagnostics
        if self.candidate_analysis is not None:
            payload["candidate_analysis"] = self.candidate_analysis
        return payload


def _identity(path: str, repo: str | None = None) -> str:
    if ":" in path:
        return path
    return f"{repo}:{path}" if repo else path


def _label_ranks(labels: list[str], paths: list[str]) -> dict[str, int]:
    """Match legacy paths across repos and qualified labels exactly.

    Each label uses its first matching rank. Overlapping labels may share a
    rank, but that rank contributes only one binary gain to nDCG. Repeated
    chunks never contribute additional gain, nor compress ranked positions.
    """
    identities: dict[str, int] = {}
    relative: dict[str, int] = {}
    for rank, identity in enumerate(paths, 1):
        identities.setdefault(identity, rank)
        relative.setdefault(identity.split(":", 1)[-1], rank)
    return {
        label: rank
        for label in labels
        if (rank := (identities if ":" in label else relative).get(label)) is not None
    }


def _validate_case(case: SearchEvalCase) -> None:
    if not isinstance(case.query, str) or not case.query.strip():
        raise ValueError("fixture query must be a nonempty string")
    if not isinstance(case.expected_paths, list) or any(
        not isinstance(p, str) or not p.strip() for p in case.expected_paths
    ):
        raise ValueError("expected_paths must be a list of nonempty strings")
    if case.repo is not None and not (
        isinstance(case.repo, str)
        and bool(case.repo.strip())
        or isinstance(case.repo, list)
        and all(isinstance(r, str) and r.strip() for r in case.repo)
    ):
        raise ValueError("repo must be a string or list of strings")
    if case.task is not None and (
        not isinstance(case.task, str) or case.task not in SEARCH_TASKS
    ):
        raise ValueError(f"invalid search evaluation task: {case.task!r}")


def compute_search_eval(
    cases: Iterable[SearchEvalCase],
    *,
    results_by_query: dict[Any, list[str]],
    k: int,
    provider: str | None = None,
    model: str | None = None,
    dimensions: int | None = None,
    latency_by_query: dict[Any, float] | None = None,
    mode: str = "semantic",
    task: str = "general",
) -> SearchEvalReport:
    """Score supplied rankings without retrieval (metadata may probe embeddings).

    String keys preserve the legacy query API. Scoped keys may be
    (query, str(repo), effective_task[, case_index]) to distinguish cases.
    Supply provider/model/dimensions for a wholly deterministic computation.
    Unlabelled cases contribute zero to macro MRR/nDCG and query hit rate.
    """
    _validate_options(k, mode, task)
    case_list = list(cases)
    for case in case_list:
        _validate_case(case)
    results = []
    for n, case in enumerate(case_list):
        key = (case.query, str(case.repo), case.task or task, n)
        paths = results_by_query.get(
            key,
            results_by_query.get(
                (case.query, str(case.repo), case.task or task),
                results_by_query.get(case.query, []),
            ),
        )
        ranked = list(paths)[:k]
        expected = list(dict.fromkeys(case.expected_paths))
        normalized = [
            (
                p
                if isinstance(p, str)
                else _identity(p.relative_path, getattr(p, "repo_slug", None))
            )
            for p in ranked
        ]
        ranks = _label_ranks(expected, normalized)
        hit = [p for p in expected if p in ranks]
        results.append(
            SearchEvalResult(
                case.query,
                expected,
                normalized,
                hit,
                [p for p in expected if p not in ranks],
                (latency_by_query or {}).get(
                    key, (latency_by_query or {}).get(case.query, 0.0)
                ),
                case.repo,
                case.task or task,
            )
        )
    lexical = mode == "lexical"
    return SearchEvalReport(
        k,
        (
            provider
            if provider is not None
            else ("not_applicable" if lexical else EMBEDDING_PROVIDER)
        ),
        (
            model
            if model is not None
            else ("not_applicable" if lexical else active_embedding_model())
        ),
        (
            dimensions
            if dimensions is not None
            else (0 if lexical else embedding_dimension())
        ),
        results,
        mode,
        task,
    )


def run_search_eval(
    project: str,
    fixture_path: str | Path,
    *,
    k: int = 5,
    mode: str = "semantic",
    task: str = "general",
    diagnose: bool = False,
    analyze_candidates: bool = False,
) -> SearchEvalReport:
    """Measure fixture retrieval; case repo/task override the run defaults."""
    _validate_options(k, mode, task)
    if diagnose or analyze_candidates:
        if k > 100:
            raise ValueError("diagnostic retrieval k must be between 1 and 100")
        cases, fingerprint = _load_fixture_snapshot(fixture_path)
        snapshot = load_eval_corpus(project, cases)
        return _run_cases(
            project,
            cases,
            k=k,
            mode=mode,
            task=task,
            snapshot=snapshot,
            fixture_sha256=fingerprint,
            **({"analyze_candidates": True} if analyze_candidates else {}),
        )
    cases = load_search_eval_fixture(fixture_path)
    return _run_cases(project, cases, k=k, mode=mode, task=task)


def _run_cases(
    project: str,
    cases: list[SearchEvalCase],
    *,
    k: int,
    mode: str,
    task: str,
    snapshot: EvalCorpusSnapshot | None = None,
    fixture_sha256: str = "",
    analyze_candidates: bool = False,
) -> SearchEvalReport:
    for case in cases:
        _validate_case(case)
    rows, latency = {}, {}
    captures: list[dict[str, Any]] = []
    for i, case in enumerate(cases):
        effective_task = case.task or task
        trace: dict[str, Any] = {}
        started = time.perf_counter()
        _, found = search_project_context(
            case.query,
            project=project,
            limit=k,
            mode=mode,
            task=effective_task,
            repo=case.repo,
            **({"candidate_trace": trace} if analyze_candidates else {}),
        )
        if analyze_candidates:
            captures.append(trace)
        key = (case.query, str(case.repo), effective_task, i)
        latency[key] = round((time.perf_counter() - started) * 1000, 2)
        rows[key] = [
            (
                f"{r.repo_slug}:{r.relative_path}"
                if getattr(r, "repo_slug", None)
                else r.relative_path
            )
            for r in found
        ]
    report = compute_search_eval(
        cases,
        results_by_query=rows,
        k=k,
        mode=mode,
        task=task,
        latency_by_query=latency,
    )
    if snapshot is not None:
        results = [
            replace(
                result,
                diagnostics=snapshot.diagnose_case(
                    case, result.missing_expected_paths, i
                ),
            )
            for i, (case, result) in enumerate(zip(cases, report.results))
        ]
        report = replace(
            report,
            results=results,
            diagnostics={
                "snapshot": snapshot.to_dict(fixture_sha256),
                "coverage": {
                    status: sum(r.diagnostics["coverage"][status] for r in results)
                    for status in AVAILABILITY_STATUSES
                },
            },
        )
    if analyze_candidates:
        report = _analyze_candidates(report, cases, captures)
    return report


def _metric_summary(report: SearchEvalReport) -> dict[str, Any]:
    """Offline replay has no retrieval latency measurement."""
    return {k: v for k, v in report.summary.items() if not k.endswith("latency_ms")}


def _analyze_candidates(
    report: SearchEvalReport,
    cases: list[SearchEvalCase],
    captures: list[dict[str, Any]],
) -> SearchEvalReport:
    def score(subcases, rows, k):
        return compute_search_eval(
            subcases,
            results_by_query=rows,
            k=k,
            mode=report.mode,
            task=report.task,
            provider=report.provider,
            model=report.model,
            dimensions=report.dimensions,
        )

    def coverage(labels, paths):
        matched = _label_ranks(labels, paths)
        return {
            "expected_paths": len(labels),
            "hits": len(matched),
            "hit_paths": [label for label in labels if label in matched],
            "missing_expected_paths": [
                label for label in labels if label not in matched
            ],
        }

    replay_rows: dict[int, dict[Any, list[str]]] = {1: {}, 2: {}}
    results = []
    pool_hits = 0
    pool_available = 0
    for i, (case, result, capture) in enumerate(zip(cases, report.results, captures)):
        key = (case.query, str(case.repo), case.task or report.task, i)
        pool_paths = [row["file_identity"] for row in capture["ranked_candidates"]]
        pool = coverage(result.expected_paths, pool_paths)
        pool.update(
            {"total_available": len(pool_paths), "unique_files": len(set(pool_paths))}
        )
        pool_hits += len(pool["hit_paths"])
        pool_available += len(pool_paths)
        channels = {}
        for name, channel in capture["channels"].items():
            paths = [row["file_identity"] for row in channel["candidates"]]
            channels[name] = {
                **{key: value for key, value in channel.items() if key != "candidates"},
                "unique_files": len(set(paths)),
                **coverage(result.expected_paths, paths),
            }
        caps = {}
        for cap in (1, 2):
            paths = replay_paths(capture, cap)
            replay_rows[cap][key] = paths
            replay_report = score([case], {case.query: paths}, report.k)
            replay_result = replay_report.results[0].to_dict()
            replay_result.pop("latency_ms")
            caps[f"cap_{cap}"] = {
                **replay_result,
                "summary": _metric_summary(replay_report),
                **path_stats(paths, report.k),
            }
        analysis = {
            "case_index": i,
            "capture": capture,
            "capture_sha256": capture_sha256(capture),
            "channels": channels,
            "lexical_ties": lexical_ties(capture["channels"].get("lexical", {})),
            "candidate_pool": pool,
            "baseline": {
                **path_stats(result.result_paths, report.k),
                **coverage(result.expected_paths, result.result_paths),
            },
            "caps": caps,
        }
        results.append(replace(result, candidate_analysis=analysis))
    caps_summary = {}
    for cap in (1, 2):
        replay_report = score(cases, replay_rows[cap], report.k)
        stats = [
            path_stats(row.result_paths, report.k) for row in replay_report.results
        ]
        caps_summary[f"cap_{cap}"] = {
            **_metric_summary(replay_report),
            "short_results": sum(row["short_results"] for row in stats),
            "unfilled_slots": sum(row["unfilled_slots"] for row in stats),
            "unique_file_occurrences": sum(row["unique_files"] for row in stats),
        }
    expected = report.summary["expected_paths"]
    return replace(
        report,
        results=results,
        candidate_analysis={
            "note": "Offline caps filter the same hydrated shortlist, not default retrieval; no refill, new query, or larger candidate budget. Capture hashes describe safe captures only, not whole-corpus or model state.",
            "candidate_pool": {
                "total_available": pool_available,
                "hits": pool_hits,
                "expected_paths": expected,
                "recall": pool_hits / expected if expected else 0.0,
            },
            "caps": caps_summary,
        },
    )


def compare_search_eval(
    project: str,
    fixture_path: str | Path,
    *,
    k: int = 5,
    task: str = "general",
    diagnose: bool = False,
    include_lexical: bool = False,
    analyze_candidates: bool = False,
) -> dict[str, Any]:
    """Run the same fixture in semantic/hybrid order; deltas are candidate-baseline.

    These fixture-specific measurements are not evidence of production gains.
    """
    _validate_options(k, "semantic", task)
    if (diagnose or analyze_candidates) and k > 100:
        raise ValueError("diagnostic retrieval k must be between 1 and 100")
    options = {}
    if diagnose or analyze_candidates:
        cases, fingerprint = _load_fixture_snapshot(fixture_path)
        options = {
            "snapshot": load_eval_corpus(project, cases),
            "fixture_sha256": fingerprint,
        }
    else:
        cases = load_search_eval_fixture(fixture_path)
    if analyze_candidates:
        options["analyze_candidates"] = True
    baseline = _run_cases(
        project, cases, k=k, mode="semantic", task=task, **options
    ).to_dict()
    candidate = _run_cases(
        project, cases, k=k, mode="hybrid", task=task, **options
    ).to_dict()
    metrics = (
        "recall_at_k",
        "query_hit_rate",
        "mrr_at_k",
        "ndcg_at_k",
        "mean_latency_ms",
        "p95_latency_ms",
    )
    comparison = {
        "baseline": baseline,
        "candidate": candidate,
        "delta": {m: candidate["summary"][m] - baseline["summary"][m] for m in metrics},
    }
    if include_lexical:
        lexical = _run_cases(
            project, cases, k=k, mode="lexical", task=task, **options
        ).to_dict()
        comparison.update(
            {
                "lexical": lexical,
                "lexical_delta": {
                    m: lexical["summary"][m] - baseline["summary"][m] for m in metrics
                },
                "comparison_note": "Fixed execution order: semantic, hybrid, lexical. Deltas are hybrid-semantic and lexical-semantic; fixture measurements are not a benchmark or evidence of production gains.",
            }
        )
    return comparison


def load_search_eval_fixture(path: str | Path) -> list[SearchEvalCase]:
    text = Path(path).read_text(encoding="utf-8")
    return _parse_fixture_text(text)


def _load_fixture_snapshot(path: str | Path) -> tuple[list[SearchEvalCase], str]:
    data = Path(path).read_bytes()
    return _parse_fixture_text(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def _parse_fixture_text(text: str) -> list[SearchEvalCase]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _parse_simple_fixture(text)
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("invalid search evaluation fixture YAML") from exc
    return _cases_from_payload(payload)


def _cases_from_payload(payload: Any) -> list[SearchEvalCase]:
    if payload is None:
        return []
    if (
        not isinstance(payload, dict)
        or "queries" not in payload
        or set(payload) - {"queries", "embedding"}
    ):
        raise ValueError(
            "fixture requires queries and optional embedding metadata only"
        )
    if "embedding" in payload:
        _validate_fixture_embedding(payload["embedding"])
    items = payload["queries"]
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("fixture queries must be a list")
    cases = []
    for item in items:
        if (
            not isinstance(item, dict)
            or "query" not in item
            or set(item) - {"query", "expected_paths", "repo", "task"}
        ):
            raise ValueError("fixture case requires query and supported fields only")
        paths = item.get("expected_paths")
        case = SearchEvalCase(
            item["query"],
            [] if paths is None else paths,
            item.get("repo"),
            item.get("task"),
        )
        _validate_case(case)
        cases.append(case)
    return cases


def _validate_fixture_embedding(metadata: Any) -> None:
    """Accept legacy fixture annotations, never runtime configuration overrides."""
    if not isinstance(metadata, dict) or set(metadata) - {
        "provider",
        "model",
        "dimensions",
    }:
        raise ValueError(
            "fixture embedding must map provider/model/dimensions metadata"
        )
    for field in ("provider", "model"):
        if field in metadata and (
            not isinstance(metadata[field], str) or not metadata[field].strip()
        ):
            raise ValueError(f"fixture embedding {field} must be a nonempty string")
    if "dimensions" in metadata:
        value = metadata["dimensions"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                "fixture embedding dimensions must be a nonnegative integer"
            )


def _fixture_embedding_scalar(field: str, value: str) -> Any:
    if field == "dimensions" and value.lstrip("+-").isdigit():
        return int(value)
    return _fixture_value(value)


def _parse_simple_fixture(text: str) -> list[SearchEvalCase]:
    """Parse the documented fixture subset without an optional YAML dependency.

    Supports block cases, scalar strings, inline/block lists, and the legacy
    embedding metadata mapping. Other YAML features are rejected rather than
    silently changing evaluation labels.
    """
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    list_field: str | None = None
    payload: dict[str, Any] = {}
    section: str | None = None
    inline_empty_root = False
    inline_embedding = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw:
            raise ValueError("fixture indentation must use spaces")
        if indent == 0:
            field, separator, value = line.partition(":")
            value = value.strip()
            if (
                not separator
                or field not in {"queries", "embedding"}
                or field in payload
            ):
                raise ValueError("fixture permits queries and optional embedding only")
            section = field
            if field == "queries":
                if value not in {"", "[]"}:
                    raise ValueError("fixture queries must be a block list or []")
                payload[field] = items
                inline_empty_root = value == "[]"
            else:
                if value and not (value.startswith("{") and value.endswith("}")):
                    raise ValueError("fixture embedding must be a mapping")
                inline_embedding = bool(value)
                metadata = {}
                for part in _fixture_parts(value[1:-1]) if value else []:
                    key, separator, scalar = part.partition(":")
                    key = _fixture_scalar(key.strip())
                    if not separator or not isinstance(key, str) or key in metadata:
                        raise ValueError("invalid fixture embedding mapping")
                    metadata[key] = _fixture_embedding_scalar(key, scalar.strip())
                payload[field] = metadata
            current, list_field = None, None
            continue
        if section == "embedding":
            field, separator, value = line.partition(":")
            if (
                inline_embedding
                or indent != 2
                or not separator
                or field in payload[section]
            ):
                raise ValueError("invalid fixture embedding mapping")
            payload[section][field] = _fixture_embedding_scalar(field, value.strip())
            continue
        if section != "queries":
            raise ValueError("fixture must start with queries or embedding")
        if inline_empty_root:
            raise ValueError("unexpected content after queries: []")
        if line.startswith("- query:"):
            if indent != 2:
                raise ValueError("fixture cases must be indented two spaces")
            current = {"query": _fixture_scalar(line.split(":", 1)[1].strip())}
            items.append(current)
            list_field = None
        elif current is not None and indent == 4 and ":" in line:
            field, value = line.split(":", 1)
            if field not in {"repo", "task", "expected_paths"} or field in current:
                raise ValueError(f"unsupported or repeated fixture field: {field}")
            value = value.strip()
            list_field = (
                field if not value and field in {"repo", "expected_paths"} else None
            )
            current[field] = [] if list_field else _fixture_value(value)
        elif (
            current is not None and indent == 6 and line.startswith("- ") and list_field
        ):
            current[list_field].append(_fixture_scalar(line[2:].strip()))
        else:
            raise ValueError(f"unsupported fixture syntax: {line}")
    return _cases_from_payload(payload or None)


def _fixture_value(value: str) -> Any:
    if not value.startswith("["):
        return _fixture_scalar(value)
    if not value.endswith("]"):
        raise ValueError("unterminated fixture list")
    return [_fixture_scalar(part) for part in _fixture_parts(value[1:-1])]


def _fixture_parts(value: str) -> list[str]:
    parts, start, quote = [], 0, None
    for i in range(len(value)):
        char = value[i]
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char and (quote == "'" or value[i - 1] != "\\"):
                quote = None
        elif char == "," and quote is None:
            parts.append(value[start:i].strip())
            start = i + 1
    if quote:
        raise ValueError("unterminated fixture string")
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _fixture_scalar(value: str) -> Any:
    if value in {"", "null", "~"}:
        return None
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ValueError("unsupported quoted fixture string") from exc
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("unterminated fixture string")
        if "'" in value[1:-1].replace("''", ""):
            raise ValueError("single quotes in fixture strings must be doubled")
        return value[1:-1].replace("''", "'")
    if (
        any(c in value for c in '[]{}#&*!|>"')
        or ": " in value
        or value.lower() in {"true", "false", "yes", "no", "on", "off"}
        or value.replace(".", "", 1).lstrip("+-").isdigit()
    ):
        raise ValueError("unsupported fixture scalar; quote string values")
    return value
