from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from typing import Any, Sequence
import urllib.error
import urllib.request

from gpc.config import (
    EMBEDDING_CACHE_SIZE,
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


# maxsize=0 means "no caching" (every call runs the wrapped function), so a
# configured size of 0 disables the cache rather than making it unbounded.
@lru_cache(maxsize=EMBEDDING_CACHE_SIZE if EMBEDDING_CACHE_SIZE > 0 else 0)
def _embed_one_cached(provider: str, model: str, text: str) -> tuple[float, ...]:
    """Cache single-text embeddings keyed by (provider, model, text).

    Identical repeated queries — common when an AI client retries or refines a
    search — skip the Ollama round-trip. Returns a tuple so the cached value is
    immutable; callers copy it back into a list.
    """

    return tuple(embed_texts([text]).vectors[0])


def embed_query(text: str) -> list[float]:
    """Embed a single query string, using the per-query cache when enabled."""

    if EMBEDDING_CACHE_SIZE == 0:
        return embed_texts([text]).vectors[0]
    normalized = text.strip()
    if not normalized:
        return embed_texts([text]).vectors[0]
    return list(
        _embed_one_cached(EMBEDDING_PROVIDER, active_embedding_model(), normalized)
    )


def cache_stats() -> dict[str, Any]:
    """Return live per-query embedding-cache stats for the current process.

    Useful for observability: a high hit-rate means repeated queries are being
    served without re-embedding. Stats are process-local (the cache is in
    memory), so they reflect the running MCP server, not historical DB logs.
    """

    info = _embed_one_cached.cache_info()
    total = info.hits + info.misses
    hit_rate = round(info.hits / total, 4) if total else 0.0
    return {
        "enabled": EMBEDDING_CACHE_SIZE > 0,
        "hits": info.hits,
        "misses": info.misses,
        "hit_rate": hit_rate,
        "currsize": info.currsize,
        "maxsize": info.maxsize,
    }


def embed_texts(texts: Sequence[str]) -> EmbeddingBatch:
    if isinstance(texts, str):
        raise TypeError(
            "embed_texts expects a sequence of strings, not a single string."
        )

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
        with urllib.request.urlopen(
            request, timeout=OLLAMA_TIMEOUT_SECONDS
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise EmbeddingError(
            f"Ollama embedding request failed: {exc.code} {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise EmbeddingError(
            f"Cannot reach Ollama at {OLLAMA_HOST}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise EmbeddingError(
            f"Ollama embedding request timed out after {OLLAMA_TIMEOUT_SECONDS}s"
        ) from exc

    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or not all(
        isinstance(vector, list) for vector in vectors
    ):
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
