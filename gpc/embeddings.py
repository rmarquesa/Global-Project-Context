from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Sequence
import urllib.error
import urllib.request

from gpc.config import (
    EMBEDDING_PROVIDER,
    OLLAMA_EMBEDDING_MODEL,
    OLLAMA_HOST,
    OLLAMA_TIMEOUT_SECONDS,
    VECTOR_SIZE,
)


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingBatch:
    provider: str
    model: str
    vectors: list[list[float]]


def embed_text(text: str) -> list[float]:
    return embed_texts([text]).vectors[0]


def embed_texts(texts: Sequence[str]) -> EmbeddingBatch:
    if isinstance(texts, str):
        raise TypeError("embed_texts expects a sequence of strings, not a single string.")

    normalized = [text.strip() for text in texts]
    if not normalized:
        return EmbeddingBatch(
            provider=EMBEDDING_PROVIDER,
            model=active_embedding_model(),
            vectors=[],
        )

    if EMBEDDING_PROVIDER == "ollama":
        return _embed_with_ollama(normalized)

    raise EmbeddingError(f"Unsupported embedding provider: {EMBEDDING_PROVIDER}")


def embedding_dimension() -> int:
    if VECTOR_SIZE > 0:
        return VECTOR_SIZE

    return len(embed_text("GPC embedding dimension probe"))


def active_embedding_model() -> str:
    if EMBEDDING_PROVIDER == "ollama":
        return OLLAMA_EMBEDDING_MODEL

    return EMBEDDING_PROVIDER


def _embed_with_ollama(texts: list[str]) -> EmbeddingBatch:
    payload = json.dumps(
        {
            "model": OLLAMA_EMBEDDING_MODEL,
            "input": texts,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise EmbeddingError(f"Ollama embedding request failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise EmbeddingError(f"Cannot reach Ollama at {OLLAMA_HOST}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise EmbeddingError(f"Ollama embedding request timed out after {OLLAMA_TIMEOUT_SECONDS}s") from exc

    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or not all(isinstance(vector, list) for vector in vectors):
        raise EmbeddingError("Ollama response did not include embeddings.")
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"Ollama returned {len(vectors)} embeddings for {len(texts)} input texts."
        )

    return EmbeddingBatch(
        provider="ollama",
        model=data.get("model") or OLLAMA_EMBEDDING_MODEL,
        vectors=vectors,
    )
