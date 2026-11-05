from pathlib import Path
import builtins

import pytest

from gpc import retrieval, search_eval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/search_eval_paired.yml"
PAIR_CONTRACT = [
    ("debug", ["gpc:scripts/migrate.py"], "How can I tell", "Como descubro"),
    (
        "implement",
        ["gpc:scripts/install_mcp_clients.py"],
        "Where does the client",
        "Em que lugar o instalador",
    ),
    (
        "review",
        ["gpc:gpc/token_economy.py"],
        "What does the token savings",
        "O que o relatório de economia",
    ),
    (
        "review",
        ["gpc:gpc/mcp_observability.py"],
        "How are sensitive values",
        "Como valores sensíveis",
    ),
    (
        "debug",
        ["gpc:scripts/migrate.py"],
        "In scripts/migrate.py",
        "Em scripts/migrate.py",
    ),
    (
        "review",
        ["gpc:gpc/mcp_observability.py"],
        "What does redact_sensitive",
        "O que redact_sensitive",
    ),
    (
        "debug",
        ["gpc:gpc/registry.py"],
        "Which repository-resolution",
        "Qual comportamento de resolução",
    ),
    (
        "review",
        ["gpc:gpc/token_economy.py"],
        "How does count_tokens",
        "Como count_tokens",
    ),
]


def test_fixed_paired_fixture_contract_and_paths():
    cases = search_eval.load_search_eval_fixture(FIXTURE)
    assert len(cases) == 16
    assert len({case.query for case in cases}) == 16
    for pair_index, (task, expected_paths, en_prefix, pt_prefix) in enumerate(
        PAIR_CONTRACT
    ):
        index = pair_index * 2
        english, portuguese = cases[index : index + 2]
        assert english.expected_paths == portuguese.expected_paths
        assert english.expected_paths == expected_paths
        assert english.repo == portuguese.repo == "gpc"
        assert english.task == portuguese.task == task
        assert english.query.startswith(en_prefix)
        assert portuguese.query.startswith(pt_prefix)
        assert all(path.startswith("gpc:") for path in english.expected_paths)
        for label in english.expected_paths:
            path = label.split(":", 1)[1]
            assert not path.startswith("/") and ".." not in Path(path).parts
            assert (ROOT / path).is_file()
        retrieval.query_terms(english.query)
        retrieval.query_terms(portuguese.query)

    # The latter four pairs intentionally include a shared source literal.
    for index in range(8, 16, 2):
        english, portuguese = cases[index : index + 2]
        literals = (
            "scripts/migrate.py",
            "0010_lexical_search.sql",
            "redact_sensitive",
            "gpc/registry.py",
            "count_tokens",
        )
        assert any(
            literal in english.query and literal in portuguese.query
            for literal in literals
        )


def test_parser_and_dependency_free_fallback_agree():
    text = FIXTURE.read_text(encoding="utf-8")
    normal = search_eval._parse_fixture_text(text)
    fallback = search_eval._parse_simple_fixture(text)
    assert normal == fallback


def test_fixture_parser_works_when_yaml_dependency_is_unavailable(monkeypatch):
    real_import = builtins.__import__

    def import_without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("simulated optional dependency absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_yaml)
    assert search_eval._parse_fixture_text(FIXTURE.read_text(encoding="utf-8")) == (
        search_eval._parse_simple_fixture(FIXTURE.read_text(encoding="utf-8"))
    )


def test_query_bounds_are_enforced_without_embeddings():
    retrieval.query_terms("palavra " * 32)
    with pytest.raises(ValueError):
        retrieval.query_terms("palavra " * 33)
    with pytest.raises(ValueError):
        retrieval.query_terms("x" * 2049)
