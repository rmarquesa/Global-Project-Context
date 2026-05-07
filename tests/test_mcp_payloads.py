from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from gpc.mcp_server import (
    _error_payload,
    _json_safe,
    _project_payload,
    _repo_payload,
    _search_result_payload,
)


@dataclass
class FakeSearchResult:
    score: float
    chunk_id: str
    relative_path: str
    title: str
    chunk_type: str
    language: str
    content: str
    repo_slug: str


def test_error_payload_has_bounded_machine_readable_shape() -> None:
    payload = _error_payload(ValueError("bad input"))

    assert payload == {"type": "ValueError", "message": "bad input"}


def test_search_result_payload_bounds_content_and_keeps_repo_slug() -> None:
    payload = _search_result_payload(
        FakeSearchResult(
            score=0.9,
            chunk_id="chunk-1",
            relative_path="README.md",
            title="README.md#0",
            chunk_type="documentation",
            language="markdown",
            content="abcdef",
            repo_slug="gpc",
        ),
        content_chars=4,
    )

    assert payload["content"] == "a..."
    assert payload["repo_slug"] == "gpc"
    assert payload["relative_path"] == "README.md"


def test_search_result_payload_can_omit_content() -> None:
    payload = _search_result_payload(
        FakeSearchResult(
            0.9,
            "chunk-1",
            "README.md",
            "README.md#0",
            "documentation",
            "markdown",
            "abcdef",
            "gpc",
        ),
        content_chars=0,
    )

    assert "content" not in payload


def test_project_and_repo_payloads_stringify_ids() -> None:
    project = _project_payload(
        {
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "slug": "gpc",
            "name": "GPC",
            "root_path": "/tmp/gpc",
        }
    )
    repo = _repo_payload(
        {"id": UUID("00000000-0000-0000-0000-000000000002"), "slug": "gpc"}
    )

    assert project["id"] == "00000000-0000-0000-0000-000000000001"
    assert repo["id"] == "00000000-0000-0000-0000-000000000002"


def test_json_safe_recurses_special_values() -> None:
    payload = _json_safe(
        {
            "time": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "price": Decimal("1.23"),
            "path": Path("README.md"),
            "items": [UUID("00000000-0000-0000-0000-000000000003")],
        }
    )

    assert payload["price"] == "1.23"
    assert payload["path"] == "README.md"
    assert payload["items"] == ["00000000-0000-0000-0000-000000000003"]
