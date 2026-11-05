from pathlib import Path
import builtins

from gpc import retrieval, search_eval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/search_eval_diversity.yml"
PAIR_CONTRACT = [
    (
        "debug",
        ["gpc:gpc/embeddings.py", "gpc:gpc/config.py"],
        "How does GPC",
        "Como o GPC",
    ),
    ("review", ["gpc:gpc/project_rename.py"], "What information", "Quais informações"),
    ("debug", ["gpc:gpc/graph_query.py"], "How does the Graphify", "Como o resumo"),
    (
        "review",
        ["gpc:gpc/cli.py", "gpc:docs/operations.md"],
        "Where does the retention command",
        "Onde o comando de retenção",
    ),
    (
        "debug",
        ["gpc:gpc/embeddings.py"],
        "In gpc/embeddings.py",
        "Em gpc/embeddings.py",
    ),
    (
        "debug",
        ["gpc:gpc/graph_query.py"],
        "What does _confidence_shift",
        "O que _confidence_shift",
    ),
]


def test_fixed_diversity_fixture_contract_and_paths():
    cases = search_eval.load_search_eval_fixture(FIXTURE)
    assert len(cases) == 12
    assert len({case.query for case in cases}) == 12
    for pair_index, (task, paths, en_prefix, pt_prefix) in enumerate(PAIR_CONTRACT):
        english, portuguese = cases[pair_index * 2 : pair_index * 2 + 2]
        assert english.expected_paths == portuguese.expected_paths == paths
        assert english.repo == portuguese.repo == "gpc"
        assert english.task == portuguese.task == task
        assert english.query.startswith(en_prefix)
        assert portuguese.query.startswith(pt_prefix)
        for label in paths:
            assert label.startswith("gpc:")
            path = Path(label.split(":", 1)[1])
            assert not path.is_absolute() and ".." not in path.parts
            assert (ROOT / path).is_file()
        for case in (english, portuguese):
            assert len(case.query.split()) <= 32
            assert len(case.query) <= 2048
            retrieval.query_terms(case.query)
    for index in (8, 10):
        english, portuguese = cases[index : index + 2]
        assert any(
            token in english.query and token in portuguese.query
            for token in ("gpc/embeddings.py", "_confidence_shift")
        )


def test_fixture_parser_and_dependency_free_fallback_agree(monkeypatch):
    text = FIXTURE.read_text(encoding="utf-8")
    assert search_eval._parse_fixture_text(text) == search_eval._parse_simple_fixture(
        text
    )
    real_import = builtins.__import__

    def without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("simulated optional dependency absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_yaml)
    assert search_eval._parse_fixture_text(text) == search_eval._parse_simple_fixture(
        text
    )
