from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import time
from typing import Any, Iterable

from gpc.config import EMBEDDING_PROVIDER
from gpc.embeddings import active_embedding_model, embedding_dimension
from gpc.search import search_project_context


@dataclass(frozen=True)
class SearchEvalCase:
    query: str
    expected_paths: list[str]


@dataclass(frozen=True)
class SearchEvalResult:
    query: str
    expected_paths: list[str]
    result_paths: list[str]
    hit_paths: list[str]
    missing_expected_paths: list[str]
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchEvalReport:
    k: int
    provider: str
    model: str
    dimensions: int
    results: list[SearchEvalResult]

    @property
    def summary(self) -> dict[str, Any]:
        expected = sum(len(result.expected_paths) for result in self.results)
        hits = sum(len(result.hit_paths) for result in self.results)
        return {
            "queries": len(self.results),
            "expected_paths": expected,
            "hits": hits,
            "recall_at_k": hits / expected if expected else 0.0,
            "query_hit_rate": (
                sum(1 for result in self.results if result.hit_paths)
                / len(self.results)
                if self.results
                else 0.0
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "embedding": {
                "provider": self.provider,
                "model": self.model,
                "dimensions": self.dimensions,
            },
            "summary": self.summary,
            "results": [result.to_dict() for result in self.results],
        }


def compute_search_eval(
    cases: Iterable[SearchEvalCase],
    *,
    results_by_query: dict[str, list[str]],
    k: int,
    provider: str | None = None,
    model: str | None = None,
    dimensions: int | None = None,
    latency_by_query: dict[str, float] | None = None,
) -> SearchEvalReport:
    results: list[SearchEvalResult] = []
    for case in cases:
        result_paths = list(results_by_query.get(case.query, []))[:k]
        hits = [path for path in case.expected_paths if path in result_paths]
        missing = [path for path in case.expected_paths if path not in result_paths]
        results.append(
            SearchEvalResult(
                query=case.query,
                expected_paths=list(case.expected_paths),
                result_paths=result_paths,
                hit_paths=hits,
                missing_expected_paths=missing,
                latency_ms=(latency_by_query or {}).get(case.query, 0.0),
            )
        )
    return SearchEvalReport(
        k=k,
        provider=provider or EMBEDDING_PROVIDER,
        model=model or active_embedding_model(),
        dimensions=dimensions or embedding_dimension(),
        results=results,
    )


def run_search_eval(
    project: str, fixture_path: str | Path, *, k: int = 5
) -> SearchEvalReport:
    cases = load_search_eval_fixture(fixture_path)
    results_by_query: dict[str, list[str]] = {}
    latency_by_query: dict[str, float] = {}
    for case in cases:
        started = time.perf_counter()
        _, results = search_project_context(case.query, project=project, limit=k)
        latency_by_query[case.query] = round((time.perf_counter() - started) * 1000, 2)
        results_by_query[case.query] = [result.relative_path for result in results]
    return compute_search_eval(
        cases,
        results_by_query=results_by_query,
        k=k,
        latency_by_query=latency_by_query,
    )


def load_search_eval_fixture(path: str | Path) -> list[SearchEvalCase]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except Exception:
        return _parse_simple_fixture(text)
    payload = yaml.safe_load(text)
    return [
        SearchEvalCase(
            query=item["query"], expected_paths=list(item.get("expected_paths") or [])
        )
        for item in payload.get("queries") or []
    ]


def _parse_simple_fixture(text: str) -> list[SearchEvalCase]:
    cases: list[SearchEvalCase] = []
    current_query: str | None = None
    current_paths: list[str] = []
    in_expected = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- query:"):
            if current_query is not None:
                cases.append(SearchEvalCase(current_query, current_paths))
            current_query = _unquote(line.split(":", 1)[1].strip())
            current_paths = []
            in_expected = False
        elif line.startswith("expected_paths:"):
            in_expected = True
        elif in_expected and line.startswith("-"):
            current_paths.append(_unquote(line[1:].strip()))
    if current_query is not None:
        cases.append(SearchEvalCase(current_query, current_paths))
    return cases


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    return value
