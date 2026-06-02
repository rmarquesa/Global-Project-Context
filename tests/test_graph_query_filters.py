from __future__ import annotations

import pytest

from gpc.graph_query import (
    ALLOWED_CONFIDENCES,
    CONFIDENCE_SCORES,
    _edge_passes,
    _min_score,
    graph_neighbors,
    graph_path,
)


@pytest.mark.parametrize("confidence", ALLOWED_CONFIDENCES)
def test_min_score_maps_allowed_confidences(confidence: str) -> None:
    assert _min_score(confidence) == CONFIDENCE_SCORES[confidence]


def test_min_score_rejects_unknown_confidence() -> None:
    with pytest.raises(ValueError):
        _min_score("MAYBE")


def test_edge_passes_treats_missing_confidence_as_extracted() -> None:
    # GRAPHIFY_RELATION edges carry no confidence; they count as EXTRACTED (1.0).
    edge: dict = {}
    assert _edge_passes(edge, _min_score("EXTRACTED")) is True
    assert _edge_passes(edge, _min_score("INFERRED")) is True


def test_edge_passes_uses_explicit_score_when_present() -> None:
    edge = {"confidence_score": 0.5}
    assert _edge_passes(edge, 0.5) is True
    assert _edge_passes(edge, 0.51) is False


def test_edge_passes_falls_back_to_confidence_label() -> None:
    assert _edge_passes({"confidence": "INFERRED"}, _min_score("INFERRED")) is True
    assert _edge_passes({"confidence": "AMBIGUOUS"}, _min_score("INFERRED")) is False


@pytest.mark.parametrize("depth", [0, -1, 4, 99])
def test_graph_neighbors_rejects_out_of_range_depth(depth: int) -> None:
    # Validation happens before any Neo4j access, so this needs no live graph.
    with pytest.raises(ValueError):
        graph_neighbors("gpc", "foo", depth=depth)


def test_graph_neighbors_requires_project_slug() -> None:
    with pytest.raises(ValueError):
        graph_neighbors("", "foo")


@pytest.mark.parametrize("max_hops", [0, -3, 9, 100])
def test_graph_path_rejects_out_of_range_max_hops(max_hops: int) -> None:
    with pytest.raises(ValueError):
        graph_path("gpc", "a", "b", max_hops=max_hops)


def test_graph_path_requires_project_slug() -> None:
    with pytest.raises(ValueError):
        graph_path("", "a", "b")
