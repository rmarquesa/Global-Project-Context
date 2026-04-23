from gpc.config import COLLECTION_NAME, QDRANT_HOST, QDRANT_PORT
from gpc.embeddings import embed_texts
from scripts.init_qdrant import SEED_TEXT
from qdrant_client import QdrantClient


def main() -> None:
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    batch = embed_texts([SEED_TEXT])
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=batch.vectors[0],
        limit=5,
        with_payload=True,
    )

    if not response.points:
        raise SystemExit("No points returned from Qdrant.")

    for point in response.points:
        print(f"id={point.id} score={point.score:.4f} payload={point.payload}")

    if not any(point.id == 1 for point in response.points):
        raise SystemExit("Seed point id=1 was not found in search results.")

    print(
        f"Search test passed with {batch.provider}:{batch.model} "
        f"({len(batch.vectors[0])} dimensions)."
    )


if __name__ == "__main__":
    main()
