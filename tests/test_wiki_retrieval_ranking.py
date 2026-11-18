"""Real research-query regressions: declared identity and graph compatibility."""
import json

import pytest

from paper_agent.tools.core import ToolContext
from paper_agent.tools.wiki import search_wiki
from paper_agent.tools.wiki_hybrid import expand_wiki_relations, search_wiki_hybrid


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / 'wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    def paper(slug, title, body, **metadata):
        path = root / 'literature/papers' / (slug + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        front = '\n'.join(key + ': ' + json.dumps(value) for key, value in metadata.items())
        path.write_text('---\n' + front + '\n---\n# ' + title + '\n## Method\n' + body)
        return path
    target = paper('patch', 'A Time Series is Worth 64 Words: Long-term Forecasting with Transformers',
                   'Patches represent windows.', doi='10.1234/patch', arxiv='2211.14730')
    refs = root / 'literature/metadata/patch/references.json'
    refs.parent.mkdir(parents=True)
    refs.write_text(json.dumps({'coined_terms': [{'name': 'PatchTST'}]}))
    paper('other', 'Long-term Time Series Forecasting', 'PatchTST linear time series forecasting. ' * 30)
    paper('battery', 'Mamba battery life prediction', 'A battery lifetime forecasting task.',
          task=['life prediction'], research_field=['battery'])
    paper('rl', 'Mamba reinforcement learning', 'Mamba state space battery life prediction. ' * 30)
    graph = root / 'graph/edges.jsonl'
    graph.parent.mkdir()
    graph.write_text('')
    return root


@pytest.mark.parametrize('query', ['PatchTST', 'PatchTST linear time series forecasting',
    '10.1234/patch', '2211.14730',
    'A Time Series is Worth 64 Words: Long-term Forecasting with Transformers'])
def test_declared_identity_beats_repeated_mentions(library, query):
    result = search_wiki(ToolContext(library.parent), query, strategy='lexical',
                         include_relations=False)
    assert json.loads(result.output)['matches'][0]['wiki_path'].endswith('/patch.md')


def test_joint_topic_coverage_beats_body_keyword_repetition(library):
    result = search_wiki(ToolContext(library.parent), 'battery life prediction Mamba',
                         strategy='lexical', include_relations=False)
    assert json.loads(result.output)['matches'][0]['wiki_path'].endswith('/battery.md')


@pytest.mark.parametrize('relation', ['extends', 'supersedes'])
def test_publisher_relations_survive_discovery(library, relation):
    (library / 'graph/edges.jsonl').write_text(json.dumps({
        'from': 'paper:patch', 'to': 'paper:other', 'type': relation, 'evidence': 'Source text.',
    }) + '\n')
    result = expand_wiki_relations(ToolContext(library.parent), 'literature/papers/patch.md')
    data = json.loads(result.output)
    assert not data['warnings']
    assert data['neighbors'][0]['relation'] == relation


def test_graph_neighbors_do_not_displace_ranked_matches(library):
    (library / 'graph/edges.jsonl').write_text(json.dumps({
        'from': 'paper:patch', 'to': 'paper:rl', 'type': 'extends', 'evidence': 'Source text.',
    }) + '\n')
    result = search_wiki(ToolContext(library.parent), 'PatchTST', strategy='lexical', limit=1)
    data = json.loads(result.output)
    assert data['matches'][0]['wiki_path'].endswith('/patch.md')
    assert data['related_matches'][0]['wiki_path'].endswith('/rl.md')
    assert data['returned_count'] == 1


def test_hybrid_exact_identifier_works_without_embedding_worker(library):
    result = search_wiki_hybrid(ToolContext(library.parent), '10.1234/patch')
    assert not result.is_error
    data = json.loads(result.output)
    assert data['semantic_status'] == 'skipped_exact'
    assert data['matches'][0]['wiki_path'].endswith('/patch.md')


def test_reader_covers_all_writer_relation_types():
    from paper_agent.tools.wiki_schema import RELATION_TYPES
    from paper_agent.tools.wiki_hybrid import RELATIONS
    assert RELATION_TYPES <= RELATIONS
