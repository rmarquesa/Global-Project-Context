from __future__ import annotations

from gpc.graphify_sync import _normalize_node, _normalize_relation


def test_normalize_node_preserves_package_identity() -> None:
    node = _normalize_node(
        {
            "id": "service",
            "label": "Service",
            "package": "acme.platform",
            "qualified_name": "acme.platform.Service",
        },
        project_slug="commerce",
        repo_slug="api",
    )

    assert node["package_name"] == "acme.platform"
    assert node["qualified_name"] == "acme.platform.Service"
    assert node["id"] == "commerce:api:service"


def test_normalize_relation_accepts_graphify_endpoint_aliases() -> None:
    relation = _normalize_relation(
        {"_src": "source", "_tgt": "target", "relation": "IMPORTS"},
        project_slug="commerce",
        repo_slug="api",
        _index=0,
    )

    assert relation["source_id"] == "source"
    assert relation["target_id"] == "target"
    assert relation["relation"] == "IMPORTS"
    assert relation["confidence"] == "EXTRACTED"
