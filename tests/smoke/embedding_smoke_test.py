from gpc.embeddings import embed_texts


def main() -> None:
    batch = embed_texts(["GPC uses Ollama for local project-memory embeddings."])
    vector = batch.vectors[0]
    print(
        f"provider={batch.provider} model={batch.model} "
        f"dimensions={len(vector)} vectors={len(batch.vectors)}"
    )


if __name__ == "__main__":
    main()
