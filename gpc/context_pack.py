from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from gpc.search import annotate_search_results, search_project_context


@dataclass(frozen=True)
class ContextPackChunk:
    path: str
    title: str | None
    content: str
    score: float
    chunk_id: str | None = None
    repo_slug: str | None = None
    graph_notes: str | None = None
    retrieval_sources: tuple[str, ...] = ()
    ranking_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPack:
    query: str
    project_slug: str | None
    markdown: str
    included_chunks: int
    max_chars: int
    truncated: bool
    citations: list[dict[str, Any]]
    token_estimate: int
    warnings: list[str]
    validation_commands: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_context_pack(
    query: str,
    *,
    project: str | None = None,
    cwd: str | None = None,
    repo: str | list[str] | None = None,
    max_chunks: int = 8,
    max_chars: int = 12_000,
    include_graph: bool = False,
    mode: str = "semantic",
    task: str = "general",
) -> ContextPack:
    resolved_project, results = search_project_context(
        query,
        project=project,
        cwd=cwd,
        repo=repo,
        limit=max_chunks,
        mode=mode,
        task=task,
    )
    if include_graph:
        results = annotate_search_results(resolved_project["slug"], results)
    chunks = [
        ContextPackChunk(
            path=result.relative_path,
            title=result.title,
            content=result.content,
            score=result.score,
            chunk_id=result.chunk_id,
            repo_slug=result.repo_slug,
            graph_notes=result.graph_notes,
            retrieval_sources=getattr(result, "retrieval_sources", ()),
            ranking_signals=getattr(result, "ranking_signals", ()),
        )
        for result in results
    ]
    return render_context_pack(
        query,
        chunks,
        project_slug=resolved_project["slug"],
        max_chars=max_chars,
        validation_commands=_validation_commands(resolved_project["slug"]),
    )


def render_context_pack(
    query: str,
    chunks: Iterable[ContextPackChunk],
    *,
    project_slug: str | None = None,
    max_chars: int = 12_000,
    warnings: list[str] | None = None,
    validation_commands: list[str] | None = None,
) -> ContextPack:
    budget = max(500, max_chars)
    warnings = list(warnings or [])
    validation_commands = list(
        validation_commands
        if validation_commands is not None
        else ["./scripts/run_smoke_tests.sh"]
    )
    chunks_list = list(chunks)
    marker = "_Truncated to fit character budget._\n\n"

    def header(display_query: str, display_project: str | None) -> str:
        lines = ["# Context Pack", "", f"Query: {display_query}"]
        if display_project:
            lines.append(f"Project: {display_project}")
        return "\n".join(
            lines
            + [
                "",
                "## Project Summary",
                "Context generated from GPC retrieval.",
                "",
                "",
            ]
        )

    prefix = header(query, project_slug)
    suffix = "\n".join(
        _warnings_lines(warnings)
        + _validation_lines(validation_commands)
        + ["## Citations", ""]
    )
    empty_message = (
        "No chunks included within character budget.\n"
        if chunks_list
        else "No chunks retrieved for this query.\n"
    )
    # Compact only caller metadata, never source identities or citation records.
    # The fixed compact form is <500 characters even at the minimum budget.
    truncated = len(prefix) + len(suffix) + len(empty_message) + len(marker) > budget
    if truncated:
        prefix = header(
            _trim_content(query, max_chars=60),
            _trim_content(project_slug, max_chars=40) if project_slug else None,
        )
        suffix = "\n".join(
            _warnings_lines(["Metadata omitted to fit character budget."])
            + _validation_lines([])
            + ["## Citations", ""]
        )

    sections: list[str] = []
    citation_texts: list[str] = []
    citations: list[dict[str, Any]] = []
    # Reserve the omission notice up front; adding it cannot evict citations later.
    remaining = budget - len(prefix) - len(suffix) - len(marker)
    for index, chunk in enumerate(chunks_list, start=1):
        citation = {
            "index": index,
            "path": chunk.path,
            "title": chunk.title,
            "score": round(chunk.score, 6),
            "chunk_id": chunk.chunk_id,
            "repo_slug": chunk.repo_slug,
            "retrieval_sources": list(chunk.retrieval_sources),
            "ranking_signals": list(chunk.ranking_signals),
        }
        citation_text = _citation_lines([citation])[1] + "\n"
        heading = [f"## [{index}] {chunk.path}", f"Score: {chunk.score:.4f}"]
        if chunk.repo_slug:
            heading.append(f"Repository: {chunk.repo_slug}")
        if chunk.title:
            heading.append(f"Title: {chunk.title}")
        section_header = "\n".join(heading) + "\n\n"
        content = chunk.content.strip()
        graph = (
            "### Graph Notes\n" + chunk.graph_notes.strip() + "\n\n"
            if chunk.graph_notes
            else ""
        )
        available = remaining - len(section_header) - len(citation_text) - 2
        if available < len(content) + len(graph):
            truncated = True
            # Keep semantic evidence first. Omitted graph data is not substituted
            # for chunk content, nor cited as though it were present.
            graph = ""
            if available < len(content):
                if sections or available < 16:
                    break
                content = _trim_content(content, max_chars=available)
        if available < 0:
            truncated = True
            break
        section = section_header + content + "\n\n" + graph
        sections.append(section)
        citation_texts.append(citation_text)
        citations.append(citation)
        remaining -= len(section) + len(citation_text)
        if len(content) < len(chunk.content.strip()):
            break
    truncated = truncated or len(citations) < len(chunks_list)
    markdown = (
        prefix
        + ("".join(sections) if sections else empty_message)
        + (marker if truncated else "")
        + suffix
        + "".join(citation_texts)
    )
    return ContextPack(
        query,
        project_slug,
        markdown,
        len(citations),
        budget,
        truncated,
        citations,
        _estimate_tokens(markdown),
        warnings,
        validation_commands,
    )


def write_context_pack(pack: ContextPack, output_path: str | Path) -> Path:
    path = Path(output_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pack.markdown, encoding="utf-8")
    return path


def _trim_content(content: str, *, max_chars: int | None = None) -> str:
    normalized = content.strip()
    if max_chars is None or len(normalized) <= max_chars:
        return normalized
    if max_chars < 15:
        return "." * max(0, min(3, max_chars))
    return normalized[: max(0, max_chars - 15)].rstrip() + "\n...[truncated]"


def _citation_lines(citations: list[dict[str, Any]]) -> list[str]:
    lines = ["## Citations"]
    for citation in citations:
        title = f" — {citation['title']}" if citation.get("title") else ""
        repo = f" (repo={citation['repo_slug']})" if citation.get("repo_slug") else ""
        lines.append(
            f"[{citation['index']}] {citation['path']}{title}{repo} (score={citation['score']})"
        )
    return lines


def _warnings_lines(warnings: list[str]) -> list[str]:
    lines = ["## Known Warnings"]
    if not warnings:
        lines.append("None.")
    else:
        lines.extend(f"- {warning}" for warning in warnings)
    return lines + [""]


def _validation_lines(commands: list[str]) -> list[str]:
    return ["## Validation Commands", *[f"- `{command}`" for command in commands], ""]


def _validation_commands(project_slug: str) -> list[str]:
    return [
        f"gpc verify --project {project_slug} --quick",
        f"gpc stale --project {project_slug} --json",
        "./scripts/run_smoke_tests.sh",
    ]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
