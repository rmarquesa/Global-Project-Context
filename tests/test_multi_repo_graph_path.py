from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import uuid

from gpc.cross_repo import RULE_PACKAGE_IMPORT, build_bridges, clear_bridges
from gpc.graph import neo4j_driver
from gpc.graph_query import graph_path
from gpc.graphify_sync import sync_graphify_to_neo4j

TEST_PROJECT_SLUG = 'consolidation-demo'
REPO_A = 'worker-a'
REPO_B = 'worker-b'
_STABLE_ID_PATTERN = re.compile(r'^consolidation-demo:(worker-a|worker-b):[a-z0-9_\-]+$')


def test_graph_path_crosses_single_bridge_between_repos() -> None:
    repo_a_root = _make_synthetic_repo(
        slug=REPO_A,
        graph={
            'nodes': [
                {
                    'id': 'worker_a_entrypoint',
                    'label': 'WorkerAEntrypoint',
                    'file_type': 'code',
                    'source_file': 'src/worker_a.py',
                },
                {
                    'id': 'shared_contract',
                    'label': 'SharedContract',
                    'file_type': 'code',
                    'source_file': 'src/contracts/shared_contract.py',
                    'package_name': 'consolidation.contracts',
                    'module_path': 'shared/shared_contract',
                    'qualified_name': 'consolidation.contracts.SharedContract',
                },
            ],
            'links': [
                {
                    'source': 'worker_a_entrypoint',
                    'target': 'shared_contract',
                    'relation': 'USES',
                }
            ],
        },
    )
    repo_b_root = _make_synthetic_repo(
        slug=REPO_B,
        graph={
            'nodes': [
                {
                    'id': 'worker_b_entrypoint',
                    'label': 'WorkerBEntrypoint',
                    'file_type': 'code',
                    'source_file': 'src/worker_b.py',
                },
                {
                    'id': 'shared_contract',
                    'label': 'SharedContract',
                    'file_type': 'code',
                    'source_file': 'lib/contracts/shared_contract.py',
                    'package_name': 'consolidation.contracts',
                    'module_path': 'shared/shared_contract',
                    'qualified_name': 'consolidation.contracts.SharedContract',
                },
            ],
            'links': [
                {
                    'source': 'worker_b_entrypoint',
                    'target': 'shared_contract',
                    'relation': 'USES',
                }
            ],
        },
    )

    node_a_id = f'{TEST_PROJECT_SLUG}:{REPO_A}:worker_a_entrypoint'
    node_b_id = f'{TEST_PROJECT_SLUG}:{REPO_B}:worker_b_entrypoint'

    try:
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        sync_graphify_to_neo4j(
            repo_a_root,
            project_slug=TEST_PROJECT_SLUG,
            repo_slug=REPO_A,
            clear_repo=True,
        )
        sync_graphify_to_neo4j(
            repo_b_root,
            project_slug=TEST_PROJECT_SLUG,
            repo_slug=REPO_B,
            clear_repo=True,
        )

        stats = build_bridges(
            TEST_PROJECT_SLUG,
            rules=(RULE_PACKAGE_IMPORT,),
            clear_existing=True,
        )
        assert stats.pairs_package_import == 1
        assert stats.edges_written == 1

        result = graph_path(TEST_PROJECT_SLUG, node_a_id, node_b_id)

        assert result['path'] is not None, result
        path = result['path']
        assert path['length'] == 3, path
        assert path['nodes'][0]['id'] == node_a_id
        assert path['nodes'][-1]['id'] == node_b_id

        bridge_hops = [hop for hop in path['hops'] if hop['relation'] == 'CROSS_REPO_BRIDGE']
        relation_hops = [hop for hop in path['hops'] if hop['relation'] == 'GRAPHIFY_RELATION']
        assert len(bridge_hops) == 1, path
        assert len(relation_hops) == 2, path
        assert bridge_hops[0]['rule'] == RULE_PACKAGE_IMPORT

        for node in path['nodes']:
            assert _STABLE_ID_PATTERN.match(node['id']), node
            assert node['id'].count(':') == 2, node
            assert node['id'].split(':', 2)[0] == TEST_PROJECT_SLUG, node
            assert node['id'].split(':', 2)[1] in {REPO_A, REPO_B}, node
    finally:
        clear_bridges(TEST_PROJECT_SLUG)
        _cleanup_graphify_project(TEST_PROJECT_SLUG)
        shutil.rmtree(repo_a_root, ignore_errors=True)
        shutil.rmtree(repo_b_root, ignore_errors=True)


def _make_synthetic_repo(*, slug: str, graph: dict) -> Path:
    base_dir = Path(__file__).resolve().parent / '_runtime'
    repo_root = base_dir / f'multi-repo-{slug}-{uuid.uuid4().hex}'
    graph_dir = repo_root / 'graphify-out'
    graph_dir.mkdir(parents=True, exist_ok=False)
    (graph_dir / 'graph.json').write_text(
        json.dumps(graph),
        encoding='utf-8',
    )
    return repo_root


def _cleanup_graphify_project(project_slug: str) -> None:
    with neo4j_driver() as driver:
        with driver.session() as session:
            session.run(
                """
                MATCH (p:GraphifyProject {slug: $slug})
                OPTIONAL MATCH (p)-[:HAS_REPO]->(r:GraphifyRepo)
                OPTIONAL MATCH (r)-[:HAS_GRAPH_NODE]->(n:GraphifyNode)
                DETACH DELETE n, r, p
                """,
                slug=project_slug,
            )
