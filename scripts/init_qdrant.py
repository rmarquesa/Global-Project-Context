import argparse
from pathlib import Path

from gpc.config import (
    COLLECTION_NAME,
    EMBEDDING_PROVIDER,
    QDRANT_HOST,
    QDRANT_PORT,
    ROOT_DIR,
)
from gpc.embeddings import active_embedding_model, embed_texts
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams


SEED_TEXT = "Initializes the Global Project Context Qdrant memory collection."


def build_seed_point() -> PointStruct:
    batch = embed_texts([SEED_TEXT])
    vector = batch.vectors[0]
    return PointStruct(
        id=1,
        vector=vector,
        payload={
            "embedding_provider": batch.provider,
            "embedding_model": batch.model,
            "embedding_dimensions": len(vector),
            "embedding_text": SEED_TEXT,
            "project_id": "gpc",
            "project_slug": "system",
            "project_name": "GPC",
            "root_path": str(ROOT_DIR),
            "file_path": "scripts/init_qdrant.py",
            "chunk_type": "bootstrap",
            "summary": SEED_TEXT,
        },
    )


def collection_vector_size(client: QdrantClient) -> int | None:
    vectors_config = client.get_collection(COLLECTION_NAME).config.params.vectors
    return getattr(vectors_config, "size", None)


def create_collection(client: QdrantClient, vector_size: int) -> None:
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )
    print(
        f"Collection '{COLLECTION_NAME}' created with vector size {vector_size} "
        f"for {EMBEDDING_PROVIDER}:{active_embedding_model()}."
    )


def reset_collection(client: QdrantClient, vector_size: int) -> None:
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
        print(f"Collection '{COLLECTION_NAME}' deleted.")

    create_collection(client, vector_size)


def ensure_collection(client: QdrantClient, vector_size: int) -> None:
    if not client.collection_exists(COLLECTION_NAME):
        create_collection(client, vector_size)
        return

    existing_size = collection_vector_size(client)
    if existing_size != vector_size:
        raise SystemExit(
            f"Collection '{COLLECTION_NAME}' uses vector size {existing_size}, "
            f"but {EMBEDDING_PROVIDER}:{active_embedding_model()} produced {vector_size}. "
            "Run `./venv/bin/python -m scripts.init_qdrant --reset` to recreate it."
        )

    print(f"Collection '{COLLECTION_NAME}' already exists with vector size {existing_size}.")


def upsert_seed_point(client: QdrantClient, seed_point: PointStruct) -> None:
    client.upsert(collection_name=COLLECTION_NAME, points=[seed_point])
    print("Seed point inserted with Ollama embedding.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the Qdrant project memory collection.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate the collection before inserting the seed point.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    seed_point = build_seed_point()
    vector_size = len(seed_point.vector)

    if args.reset:
        reset_collection(client, vector_size)
    else:
        ensure_collection(client, vector_size)

    upsert_seed_point(client, seed_point)


if __name__ == "__main__":
    main()
