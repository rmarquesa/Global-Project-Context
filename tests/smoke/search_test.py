from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient

from gpc.config import COLLECTION_NAME, QDRANT_HOST, QDRANT_PORT
from scripts.init_qdrant import build_seed_point, upsert_seed_point


def main() -> None:
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    seed_payload = _refresh_seed_point(client)
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=seed_payload["vector"],
        limit=5,
        with_payload=True,
    )

    if not response.points:
        raise SystemExit("No points returned from Qdrant.")

    for point in response.points:
        print(f"id={point.id} score={point.score:.4f} payload={point.payload}")

    seed_point = next((point for point in response.points if point.id == 1), None)
    if seed_point is None:
        raise SystemExit("Seed point id=1 was not found in search results.")

    _assert_seed_payload(
        seed_point.payload or {},
        provider=seed_payload["provider"],
        model=seed_payload["model"],
        dimensions=seed_payload["dimensions"],
    )

    print(
        f"Search test passed with {seed_payload['provider']}:{seed_payload['model']} "
        f"({seed_payload['dimensions']} dimensions)."
    )


def _refresh_seed_point(client: QdrantClient) -> dict[str, Any]:
    # This live-service smoke test intentionally mutates Qdrant so the bootstrap
    # seed matches the active embedding provider/model before querying it.
    seed_point = build_seed_point()
    upsert_seed_point(client, seed_point)
    payload = seed_point.payload or {}
    return {
        "vector": seed_point.vector,
        "provider": payload["embedding_provider"],
        "model": payload["embedding_model"],
        "dimensions": payload["embedding_dimensions"],
    }


def _assert_seed_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
    dimensions: int,
) -> None:
    expected = {
        "project_id": "gpc",
        "project_slug": "system",
        "source_type": "bootstrap",
        "embedding_provider": provider,
        "embedding_model": model,
        "embedding_dimensions": dimensions,
    }
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise SystemExit(f"Seed point payload mismatch: {mismatches}")


if __name__ == "__main__":
    main()
