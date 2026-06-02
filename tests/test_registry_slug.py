from __future__ import annotations

import pytest

from gpc.registry import SLUG_RE, normalize_slug


def test_normalize_slug_lowercases_and_keeps_valid_chars() -> None:
    assert normalize_slug("MyProject") == "myproject"
    assert normalize_slug("aluga_facil") == "aluga_facil"
    assert normalize_slug("graph-query") == "graph-query"


def test_normalize_slug_replaces_runs_of_invalid_chars_with_single_dash() -> None:
    assert normalize_slug("My Project!!") == "my-project"
    assert normalize_slug("a/b\\c") == "a-b-c"
    assert normalize_slug("  spaced  name  ") == "spaced-name"


def test_normalize_slug_strips_leading_and_trailing_separators() -> None:
    assert normalize_slug("--edge--") == "edge"
    assert normalize_slug("__under__") == "under"
    assert normalize_slug("...dots...") == "dots"


@pytest.mark.parametrize("value", ["", "!!!", "---", "___", "  "])
def test_normalize_slug_rejects_empty_or_separator_only(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_slug(value)


def test_normalized_slug_always_matches_slug_re() -> None:
    for raw in ["MyProject", "My Project!!", "a/b\\c", "graph-query", "123abc"]:
        assert SLUG_RE.match(normalize_slug(raw))
