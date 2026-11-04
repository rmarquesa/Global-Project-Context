from unittest.mock import MagicMock

import pytest

from gpc.search import SearchResult, compose_project_context


def test_compose_context_is_bounded_and_retains_repo_for_same_path(monkeypatch):
    from gpc import search

    project = {"slug": "demo", "name": "Demo", "id": "id"}
    results = [
        SearchResult(0.9, "1", "README.md", "API", "API evidence", "file", None, "api"),
        SearchResult(0.8, "2", "README.md", "Web", "Web evidence", "file", None, "web"),
    ]
    monkeypatch.setattr(
        search, "search_project_context", lambda *a, **k: (project, results)
    )
    monkeypatch.setattr(
        search,
        "_graph_annotations",
        lambda **k: {1: "Graph neighbours: API", 2: "Graph neighbours: Web"},
    )
    _, returned, block = compose_project_context(
        "q", max_chars=1000, include_graph=True
    )
    assert len(block) <= 1000
    assert [r.repo_slug for r in returned] == ["api", "web"]
    assert returned[0].graph_notes == "Graph neighbours: API"
    assert "[1] README.md (repo=api," in block
    assert "[2] README.md (repo=web," in block


def test_graph_failure_degrades_gracefully(monkeypatch):
    from gpc import search
    import gpc.graph_query as graph_query

    monkeypatch.setattr(
        graph_query,
        "graph_neighbors",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()),
    )
    monkeypatch.setattr(search, "_graph_source_id", lambda *a: "scoped-node-id")
    # Graph lookup exceptions are swallowed by _graph_annotations.
    result = SearchResult(0.9, "1", "a.py", None, "x", "file", None, "repo")
    assert (
        search._graph_annotations(
            project_slug="p", results=[result], min_confidence="EXTRACTED"
        )
        == {}
    )


@pytest.mark.parametrize("kind", ["query", "project", "source", "content", "graph"])
def test_compose_strict_effective_budget(monkeypatch, kind):
    from gpc import search

    project = {"slug": "p", "name": "Project"}
    query = "q"
    if kind == "query":
        query *= 10000
    if kind == "project":
        project = {"slug": "p" * 10000, "name": "n" * 10000}
    result = SearchResult(
        0.9,
        "1",
        "README.md" if kind != "source" else "p" * 10000,
        None,
        "useful" if kind != "content" else "c" * 10000,
        "file",
        None,
        "api",
    )
    monkeypatch.setattr(
        search, "search_project_context", lambda *a, **k: (project, [result])
    )
    monkeypatch.setattr(
        search,
        "_graph_annotations",
        lambda **k: {1: "g" * 10000} if kind == "graph" else {},
    )
    _, _, block = compose_project_context(query, max_chars=100, include_graph=True)
    assert len(block) <= 1000  # public floor is retained
    assert "...[truncated]" in block
    if kind == "source":
        assert "[1]" not in block
    if kind == "graph":
        assert "useful" in block
        assert "gggggg" not in block


def test_build_graph_annotations_use_one_search_and_repo_scoped_nodes(monkeypatch):
    from gpc import context_pack, graph, graph_query

    results = [
        SearchResult(
            0.9, str(i), "README.md", None, f"{repo} evidence", "file", None, repo
        )
        for i, repo in enumerate(["api", "web", "api"])
    ]
    semantic = MagicMock(return_value=({"slug": "project"}, results))
    monkeypatch.setattr(context_pack, "search_project_context", semantic)
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    scoped_calls = []

    def resolve(cypher, **params):
        assert "n.project_slug = $project" in cypher
        assert "n.repo_slug = $repo" in cypher
        scoped_calls.append(params)
        return MagicMock(single=lambda: {"id": f"project:{params['repo']}:README"})

    session.run.side_effect = resolve
    monkeypatch.setattr(
        graph,
        "neo4j_driver",
        lambda: MagicMock(__enter__=lambda self: driver, __exit__=lambda *a: None),
    )
    neighbor_calls = []

    def neighbors(project, node, **kwargs):
        assert project == "project"
        assert kwargs["min_confidence"] == "EXTRACTED"
        neighbor_calls.append(node)
        repo = node.split(":")[1]
        return {
            "neighbors": [
                {
                    "label": f"{repo}-only-neighbor",
                    "repo_slug": repo,
                    "edges": [{"relation": "CALLS"}],
                }
            ]
        }

    monkeypatch.setattr(graph_query, "graph_neighbors", neighbors)
    pack = context_pack.build_context_pack("q", include_graph=True)
    semantic.assert_called_once()
    assert [c["repo"] for c in scoped_calls] == ["api", "web"]
    assert neighbor_calls == ["project:api:README", "project:web:README"]
    api_section = pack.markdown.split("## [1]")[1].split("## [2]")[0]
    web_section = pack.markdown.split("## [2]")[1].split("## [3]")[0]
    assert "api-only-neighbor" in api_section and "web-only-neighbor" not in api_section
    assert "web-only-neighbor" in web_section and "api-only-neighbor" not in web_section
    assert "EXTRACTED" in pack.markdown
    assert pack.markdown.count("api-only-neighbor") == 2


def test_build_graph_failure_keeps_semantic_chunks_without_retry(monkeypatch):
    from gpc import context_pack, graph

    semantic = MagicMock(
        return_value=(
            {"slug": "p"},
            [
                SearchResult(
                    0.9, "1", "README.md", None, "useful evidence", "file", None, "api"
                )
            ],
        )
    )
    monkeypatch.setattr(context_pack, "search_project_context", semantic)
    monkeypatch.setattr(
        graph, "neo4j_driver", MagicMock(side_effect=RuntimeError("offline"))
    )
    pack = context_pack.build_context_pack("q", include_graph=True)
    semantic.assert_called_once()
    assert pack.included_chunks == 1
    assert "useful evidence" in pack.markdown
    assert "Graph Notes" not in pack.markdown
