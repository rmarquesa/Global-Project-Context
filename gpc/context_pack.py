from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from gpc.search import compose_project_context, search_project_context


@dataclass(frozen=True)
class ContextPackChunk:
    path: str
    title: str | None
    content: str
    score: float
    chunk_id: str | None = None
    repo_slug: str | None = None
    graph_notes: str | None = None


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
) -> ContextPack:
    resolved_project, results = search_project_context(
        query,
        project=project,
        cwd=cwd,
        repo=repo,
        limit=max_chunks,
    )
    graph_notes_by_path: dict[str, str] = {}
    warnings: list[str] = []
    if include_graph:
        try:
            _, _, context_block = compose_project_context(
                query,
                project=project,
                cwd=cwd,
                repo=repo,
                max_chunks=max_chunks,
                max_chars=max_chars,
                include_graph=True,
            )
            graph_notes_by_path = _extract_graph_notes(context_block)
        except Exception as exc:
            warnings.append(f"Graph notes unavailable: {exc}")
    chunks = [
        ContextPackChunk(
            path=result.relative_path,
            title=result.title,
            content=result.content,
            score=result.score,
            chunk_id=result.chunk_id,
            repo_slug=result.repo_slug,
            graph_notes=graph_notes_by_path.get(result.relative_path),
        )
        for result in results
    ]
    return render_context_pack(
        query,
        chunks,
        project_slug=resolved_project["slug"],
        max_chars=max_chars,
        warnings=warnings,
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
    citations: list[dict[str, Any]] = []
    warnings = warnings or []
    validation_commands = validation_commands or ["./scripts/run_smoke_tests.sh"]
    lines = ["# Context Pack", "", f"Query: {query}"]
    if project_slug:
        lines.append(f"Project: {project_slug}")
    lines.extend(
        ["", "## Project Summary", "Context generated from GPC semantic retrieval.", ""]
    )

    chunks_list = list(chunks)
    if not chunks_list:
        lines.append("No chunks retrieved for this query.")
        lines.extend(_warnings_lines(warnings))
        lines.extend(_validation_lines(validation_commands))
        markdown = "\n".join(lines).strip() + "\n"
        return ContextPack(
            query,
            project_slug,
            markdown,
            0,
            budget,
            False,
            [],
            _estimate_tokens(markdown),
            warnings,
            validation_commands,
        )

    truncated = False
    included = 0
    body_lines = list(lines)
    for index, chunk in enumerate(chunks_list, start=1):
        citation = {
            "index": index,
            "path": chunk.path,
            "title": chunk.title,
            "score": round(chunk.score, 6),
            "chunk_id": chunk.chunk_id,
            "repo_slug": chunk.repo_slug,
        }
        section = [f"## [{index}] {chunk.path}", f"Score: {chunk.score:.4f}"]
        if chunk.title:
            section.append(f"Title: {chunk.title}")
        section.extend(["", _trim_content(chunk.content), ""])
        if chunk.graph_notes:
            section.extend(["### Graph Notes", chunk.graph_notes.strip(), ""])
        candidate = "\n".join(
            body_lines
            + section
            + _warnings_lines(warnings)
            + _validation_lines(validation_commands)
            + _citation_lines(citations + [citation])
        )
        if len(candidate) > budget:
            remaining = (
                budget - len("\n".join(body_lines + _citation_lines(citations))) - 250
            )
            if remaining > 120 and included == 0:
                section = section.copy()
                section[-2] = _trim_content(chunk.content, max_chars=remaining)
                body_lines.extend(section)
                citations.append(citation)
                included += 1
            truncated = True
            break
        body_lines.extend(section)
        citations.append(citation)
        included += 1

    if truncated:
        body_lines.append("_Truncated to fit character budget._")
        body_lines.append("")
    body_lines.extend(_warnings_lines(warnings))
    body_lines.extend(_validation_lines(validation_commands))
    body_lines.extend(_citation_lines(citations))
    markdown = "\n".join(body_lines).strip() + "\n"
    return ContextPack(
        query,
        project_slug,
        markdown,
        included,
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
    return normalized[: max(0, max_chars - 15)].rstrip() + "\n...[truncated]"


def _citation_lines(citations: list[dict[str, Any]]) -> list[str]:
    lines = ["## Citations"]
    for citation in citations:
        title = f" — {citation['title']}" if citation.get("title") else ""
        lines.append(
            f"[{citation['index']}] {citation['path']}{title} (score={citation['score']})"
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


def _extract_graph_notes(context_block: str) -> dict[str, str]:
    notes: dict[str, str] = {}
    current_path: str | None = None
    collecting = False
    buffer: list[str] = []
    for raw_line in context_block.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and "]" in line and "path=" in line:
            if current_path and buffer:
                notes[current_path] = "\n".join(buffer).strip()
            current_path = line.split("path=", 1)[1].split()[0]
            buffer = []
            collecting = False
        elif line.lower().startswith("graph neighbours") or line.lower().startswith(
            "graph neighbors"
        ):
            collecting = True
            buffer.append(raw_line)
        elif collecting:
            if line.startswith("[") and "]" in line and "path=" in line:
                collecting = False
            else:
                buffer.append(raw_line)
    if current_path and buffer:
        notes[current_path] = "\n".join(buffer).strip()
    return notes
