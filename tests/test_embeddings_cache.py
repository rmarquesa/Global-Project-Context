from __future__ import annotations

import gpc.embeddings as embeddings
from gpc.embeddings import EmbeddingBatch


def _fake_batch(texts):
    # One deterministic 3-d vector per input text.
    return EmbeddingBatch(
        provider="fake",
        model="fake-model",
        vectors=[[float(len(t)), 1.0, 2.0] for t in texts],
    )


def test_embed_query_caches_identical_queries(monkeypatch) -> None:
    calls: list[list[str]] = []

    def counting_embed(texts):
        calls.append(list(texts))
        return _fake_batch(texts)

    embeddings._embed_one_cached.cache_clear()
    monkeypatch.setattr(embeddings, "embed_texts", counting_embed)
    monkeypatch.setattr(embeddings, "EMBEDDING_CACHE_SIZE", 256)

    first = embeddings.embed_query("who calls auth?")
    second = embeddings.embed_query("who calls auth?")

    assert first == second
    # The Ollama-backed embed_texts must run only once for repeated queries.
    assert len(calls) == 1


def test_embed_query_distinguishes_different_queries(monkeypatch) -> None:
    calls: list[list[str]] = []

    def counting_embed(texts):
        calls.append(list(texts))
        return _fake_batch(texts)

    embeddings._embed_one_cached.cache_clear()
    monkeypatch.setattr(embeddings, "embed_texts", counting_embed)
    monkeypatch.setattr(embeddings, "EMBEDDING_CACHE_SIZE", 256)

    embeddings.embed_query("question one")
    embeddings.embed_query("question two")

    assert len(calls) == 2


def test_embed_query_returns_a_fresh_list(monkeypatch) -> None:
    embeddings._embed_one_cached.cache_clear()
    monkeypatch.setattr(embeddings, "embed_texts", _fake_batch)
    monkeypatch.setattr(embeddings, "EMBEDDING_CACHE_SIZE", 256)

    vec = embeddings.embed_query("mutate me")
    vec.append(999.0)
    # Mutating the returned list must not corrupt the cached tuple.
    assert embeddings.embed_query("mutate me") == [len("mutate me"), 1.0, 2.0]
