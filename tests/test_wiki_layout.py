"""Source partitions share graph identities and keep historical links readable."""
import json
from pathlib import Path

from paper_agent.api import library
from paper_agent.tools import research_wiki
from paper_agent.tools.wiki_layout import resolve_legacy_path


def test_graph_reads_all_nodes_and_large_relationship_file(tmp_path, monkeypatch):
    research_wiki.init_wiki(str(tmp_path), separate_origins=True)
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(tmp_path))
    for i in range(1005):
        corpus = 'autoresearch' if i == 1004 else 'literature'
        (tmp_path / corpus / 'papers' / f'{i:04}.md').write_text(
            f'---\nnode_id: paper:{i}\n---\n# Paper {i}\n')
    edge = {'from': 'paper:0', 'to': 'paper:1004', 'type': 'extends',
            'evidence': 'source evidence ' * 150000}
    (tmp_path / 'graph/edges.jsonl').write_text(json.dumps(edge) + '\n')
    result = library.paradigm_graph()
    assert len(result['nodes']) == 1005 and not result['truncated']
    assert result['edges'][0]['target'] == 'paper:1004'
    assert len(result['edges'][0]['evidence']) == 2000


def test_global_index_and_log_cover_both_origins(tmp_path):
    research_wiki.init_wiki(str(tmp_path), separate_origins=True)
    for origin in ('literature', 'autoresearch'):
        paper = tmp_path / origin / 'papers' / f'{origin}.md'
        paper.write_text(f'---\nnode_id: paper:{origin}\ntitle: {origin}\n---\n# {origin}\n')
    research_wiki.rebuild_index(str(tmp_path / 'autoresearch'))
    text = (tmp_path / 'index.md').read_text()
    assert 'literature/papers/literature.md' in text
    assert 'autoresearch/papers/autoresearch.md' in text
    research_wiki.append_log(str(tmp_path / 'autoresearch'), 'published')
    assert 'published' in (tmp_path / 'log.md').read_text()
    assert resolve_legacy_path(tmp_path, 'papers/autoresearch.md') == tmp_path / 'autoresearch/papers/autoresearch.md'
    assert resolve_legacy_path(tmp_path / 'literature', 'papers/literature.md') == tmp_path / 'literature/papers/literature.md'


def test_renamed_paths_remain_readable_from_old_names(tmp_path, monkeypatch):
    research_wiki.init_wiki(str(tmp_path), separate_origins=True)
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(tmp_path))
    old = 'literature/papers/马晨2026_lean.md'
    new = 'literature/papers/ma2026_lean.md'
    target = tmp_path / new
    target.write_text('---\nnode_id: paper:ma2026_lean\n---\n# A lean framework\n')
    (tmp_path / 'graph/path_aliases.json').write_text(json.dumps({old: new}))
    for root, relative in [(tmp_path, old), (tmp_path, 'papers/马晨2026_lean.md'),
                           (tmp_path / 'literature', 'papers/马晨2026_lean.md')]:
        assert resolve_legacy_path(root, relative) == target
    assert library.read_document('paradigms', old)['path'] == new
