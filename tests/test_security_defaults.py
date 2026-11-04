from __future__ import annotations

from pathlib import Path
import re

import pytest

from gpc import indexer


def test_every_compose_published_port_is_loopback_including_optional_profiles() -> None:
    compose = (Path(__file__).parents[1] / "docker-compose.yaml").read_text()
    services = {}
    service = None
    in_ports = False
    for line in compose.splitlines():
        match = re.fullmatch(r"  ([\w-]+):", line)
        if match:
            service = match.group(1)
            in_ports = False
        if line == "    ports:":
            in_ports = True
            services[service] = []
        elif in_ports and line.startswith("      - "):
            # Inspect every declaration, not just known ports; Docker is not needed.
            value = line.removeprefix("      - ").strip("\"'")
            assert re.fullmatch(r"127\.0\.0\.1:\$\{GPC_\w+_PORT:-\d+\}:\d+", value)
            services[service].append(value)
        elif in_ports:
            in_ports = False

    assert {name: len(ports) for name, ports in services.items()} == {
        "postgres": 1,
        "qdrant": 2,
        "neo4j": 2,
        "gpc-mcp-http": 1,
        "grafana": 1,
    }
    assert "      GPC_MCP_HTTP_HOST: 0.0.0.0\n" in compose
    assert "      GPC_QDRANT_HOST: qdrant\n" in compose
    assert "@postgres:5432/" in compose


@pytest.mark.parametrize("offset", [199_900, 199_990, 200_100, 511_900])
@pytest.mark.parametrize(
    "marker",
    ["-----BEGIN PRIVATE KEY-----", "AKIA" + "X" * 16, "api_key=" + "z" * 30],
    ids=["synthetic-pem-header", "synthetic-access-id", "synthetic-assignment"],
)
def test_complete_accepted_file_is_checked_before_embedding_or_persistence(
    tmp_path, monkeypatch, offset, marker
) -> None:
    # Deliberately inert marker strings, not credentials. Includes a marker
    # crossing the historical 200,000-character prefix and one near the file end.
    content = (" " * offset + marker + "\n").ljust(512_000)
    path = tmp_path / "notes.txt"
    path.write_text(content)
    assert indexer._looks_sensitive_content(content)
    result = indexer.discover_project_files(tmp_path, indexer.IndexOptions())
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.size_bytes == indexer.IndexOptions().max_file_bytes

    def forbidden(*args, **kwargs):
        pytest.fail("Sensitive content reached embedding or content persistence")

    removed = []

    def remove_existing(project, relative_path, qdrant):
        # Cleanup is allowed; no source content is passed to it.
        removed.append((project["id"], relative_path))
        return indexer.FileIndexResult(
            status="removed", chunks_deleted=1, points_deleted=1
        )

    monkeypatch.setattr(indexer, "embed_texts", forbidden)
    monkeypatch.setattr(indexer, "_connect", forbidden)
    monkeypatch.setattr(indexer, "_upsert_file", forbidden)
    monkeypatch.setattr(indexer, "_remove_file_index", remove_existing)
    indexed = indexer._index_file(
        {"id": "synthetic-project"}, candidate, object(), indexer.IndexOptions()
    )

    assert indexed.status == "skipped"
    assert indexed.reason == "sensitive_content"
    assert indexed.chunks_deleted == indexed.points_deleted == 1
    assert removed == [("synthetic-project", "notes.txt")]
