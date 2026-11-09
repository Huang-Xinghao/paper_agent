import json

from paper_agent.agent.loop import PhaseAgent
from paper_agent.tools import ToolContext, invoke


def test_wiki_search_and_cards_are_wiki_scoped(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("PAPER_AGENT_WIKI_ROOT", str(tmp_path / "research-wiki"))
    page = tmp_path / "research-wiki/papers/smith2025_battery.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Battery paper\n\n## Method\nVoltage features and state estimation.\n")
    (tmp_path / "secret.md").write_text("OutsideSecret")
    ctx = ToolContext(tmp_path)

    result = asyncio.run(invoke("search_wiki", ctx, query="voltage features"))
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["matches"][0]["path"] == "research-wiki/papers/smith2025_battery.md"
    method = next(f for f in payload["cards"][0]["fields"] if f["section"] == "Method")
    assert method["content"] == "Voltage features and state estimation."
    assert json.loads(asyncio.run(invoke("search_wiki", ctx, query="OutsideSecret")).output)["count"] == 0


def test_paper_agent_exposes_wiki_tools_in_research_phases(tmp_path):
    class Model:
        def complete(self, messages, tools):
            return type("Response", (), {"content": "done", "tool_calls": ()})()

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="ideas")
    names = {spec.name for spec in agent.tools}
    assert "search_wiki" in names
    assert not {"read_wiki", "browse_wiki", "expand_wiki_relations"} & names
    assert "research-wiki" in agent._system_prompt()


def test_default_wiki_root_is_repository_root(monkeypatch):
    from pathlib import Path
    from paper_agent.tools import wiki
    monkeypatch.delenv('PAPER_AGENT_WIKI_ROOT', raising=False)
    assert wiki.resolve_wiki_root() == Path(wiki.__file__).resolve().parents[2] / 'research-wiki'


def test_shared_wiki_search_and_cards_across_projects(tmp_path, monkeypatch):
    import asyncio
    shared = tmp_path / 'shared/research-wiki'
    page = shared / 'papers/battery.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Battery SOH\n## Method\nShared voltage evidence')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(shared))
    for name in ('demo8', 'demo9'):
        project = tmp_path / name
        project.mkdir()
        local = project / 'research-wiki/papers/local.md'
        local.parent.mkdir(parents=True)
        local.write_text('Battery LocalOnlyMarker must not override shared wiki')
        ctx = ToolContext(project)
        payload = json.loads(asyncio.run(invoke('search_wiki', ctx, query='Battery')).output)
        matches = payload['matches']
        assert len(matches) == 1 and matches[0]['path'] == str(page)
        card = payload['cards'][0]
        assert card['wiki_path'] == 'papers/battery.md'
        assert any(f['content'] == 'Shared voltage evidence' for f in card['fields'])
        assert json.loads(asyncio.run(invoke('search_wiki', ctx, query='LocalOnlyMarker')).output)['count'] == 0


def test_runner_rejects_nonresearch_wiki_phase(tmp_path):
    import pytest
    from paper_agent.agent.workflow import WorkflowRunner
    agent = PhaseAgent(tmp_path, model_client=object())
    with pytest.raises(ValueError, match='Unsupported Wiki research phase: test'):
        WorkflowRunner(agent)._research_context('Battery SOH', stage='test')


def test_quality_issues_follow_search_and_paper_derived_nodes(tmp_path, monkeypatch):
    import asyncio
    root = tmp_path / 'research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    for folder, slug in (('papers', 'paper'), ('ideas', 'idea'), ('experiments', 'experiment')):
        page = root / folder / f'{slug}.md'
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('Battery evidence\n' + '\n'.join(f'line {i}' for i in range(300)), encoding='utf-8')
    issue = {'id': 'i1', 'origin': 'paper', 'kind': 'internal_inconsistency',
             'description': 'Synthetic batch-size conflict', 'guidance': 'Check both sources',
             'evidence': [{'block_id': 'p1-b1', 'quote': '32', 'page': 1}, {'block_id': 'p2-b1', 'quote': '16', 'page': 2}]}
    (root / 'papers/paper.quality.json').write_text(json.dumps({
        'status': 'accepted_with_issues', 'schema_version': 'wiki-quality-v2',
        'document_id': 'synthetic-doc', 'issues': [issue]}), encoding='utf-8')
    (root / 'graph').mkdir()
    (root / 'graph/edges.jsonl').write_text('\n'.join(json.dumps(e) for e in [
        {'from': 'idea:idea', 'to': 'paper:paper', 'type': 'inspired_by'},
        {'from': 'idea:idea', 'to': 'exp:experiment', 'type': 'tested_by'}]), encoding='utf-8')
    ctx = ToolContext(tmp_path)
    result = asyncio.run(invoke('search_wiki', ctx, query='Battery'))
    assert all(m['quality_records'][0]['issues'] == [issue] for m in json.loads(result.output)['matches'])
    for kind in ('papers', 'ideas', 'experiments'):
        result = asyncio.run(invoke('search_wiki', ctx, query='Battery', entity_type=kind))
        payload = json.loads(result.output)
        assert len(payload['matches']) == 1
        match = payload['matches'][0]
        assert match['quality_records'][0]['issues'] == [issue]
        assert match['wiki_path'].startswith(kind + '/')
        assert all('batch' not in excerpt['excerpt']
                   for hit in match['hits'] for excerpt in hit['excerpts'])


def test_missing_or_corrupt_quality_report_does_not_imply_pass(tmp_path, monkeypatch):
    import asyncio
    root = tmp_path / 'research-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    page = root / 'papers/legacy.md'
    page.parent.mkdir(parents=True)
    page.write_text('Legacy paper', encoding='utf-8')
    ctx = ToolContext(tmp_path)
    read = lambda: json.loads(asyncio.run(invoke('search_wiki', ctx, query='Legacy')).output)['cards'][0]
    assert read()['quality_records'][0]['status'] == 'not_checked'
    page.with_suffix('.quality.json').write_text('{', encoding='utf-8')
    assert read()['quality_records'][0]['status'] == 'unavailable'


def test_chinese_query_locators_sections_and_live_cards(tmp_path, monkeypatch):
    import asyncio
    root = tmp_path / 'wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    page = root / 'papers/example.md'
    page.parent.mkdir(parents=True)
    page.write_text('---\nnode_id: "paper:example"\n---\n# Example\n\n'
                    '## One-line thesis\n通过充后松弛预测退役电池的未来退化轨迹。\n\n'
                    '## Method\n使用当前循环特征。\n\n'
                    '## Limitations / Failure Modes\n必须给定未来负载。（PDF 第 4 页）\n', encoding='utf-8')
    ctx = ToolContext(tmp_path)
    call = lambda name, **kw: json.loads(asyncio.run(invoke(name, ctx, **kw)).output)
    found = call('search_wiki', query='没有历史循环记录如何预测退役电池的退化轨迹')
    assert found['matches'][0]['wiki_path'] == 'papers/example.md'
    hit = found['matches'][0]['hits'][0]['excerpts'][0]
    lines = page.read_text(encoding='utf-8').splitlines()
    assert hit['excerpt'] in lines[hit['line'] - 1]
    assert hit['section'] == 'One-line thesis'
    fields = found['cards'][0]['fields']
    thesis = next(f for f in fields if f['section'] == hit['section'])
    assert hit['excerpt'] in thesis['content']
    assert thesis['start_line'] == hit['start_line']
    limits = next(f for f in fields if f['section'] == 'Limitations / Failure Modes')
    assert '必须给定未来负载' in limits['content']
    assert limits['source_mentions'] == ['PDF 第 4 页']
    assert lines[limits['start_line'] - 1] == '## Limitations / Failure Modes'
    cards = call('search_wiki', query='退役电池', strategy='lexical')
    card = cards['cards'][0]
    assert card['quality_records'][0]['status'] == 'not_checked'
    assert card['fields'][3]['status'] == 'missing'  # No fabricated result.
    previous = card['content_sha256']
    page.write_text(page.read_text(encoding='utf-8').replace('当前循环特征', '最新松弛特征'), encoding='utf-8')
    updated = call('search_wiki', query='退役电池', strategy='lexical')['cards'][0]
    assert updated['content_sha256'] != previous
    assert '最新松弛特征' in updated['fields'][2]['content']


def test_default_search_groups_only_provenance_and_preserves_derived_hit(tmp_path, monkeypatch):
    import asyncio
    root = tmp_path / 'wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    contents = {'papers/p.md': '# Source\n## Method\nBase model.',
                'papers/other.md': '# Other\nUnrelated.',
                'ideas/i.md': '# Idea\n## Thesis\nRareMechanism works.',
                'experiments/e.md': '# Experiment\nRareMechanism failed.',
                'ideas/orphan.md': '# Orphan\nRareMechanism unlinked.',
                'log.md': 'RareMechanism ' * 100,
                'archive/papers/archived.md': 'RareMechanism ' * 100}
    for rel, body in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
    (root / 'graph').mkdir()
    graph = root / 'graph/edges.jsonl'
    graph.write_text('\n'.join(json.dumps(e) for e in [
        {'from': 'idea:i', 'to': 'paper:p', 'type': 'inspired_by'},
        {'from': 'idea:i', 'to': 'exp:e', 'type': 'tested_by'},
        {'from': 'paper:p', 'to': 'paper:other', 'type': 'shares_method'}]), encoding='utf-8')
    ctx = ToolContext(tmp_path)
    search = lambda **kw: json.loads(asyncio.run(invoke('search_wiki', ctx, query='RareMechanism', **kw)).output)
    payload = search()
    assert {m['wiki_path'] for m in payload['matches']} == {'papers/p.md', 'ideas/orphan.md'}
    parent = next(m for m in payload['matches'] if m['wiki_path'] == 'papers/p.md')
    assert {h['wiki_path'] for h in parent['hits']} == {'ideas/i.md', 'experiments/e.md'}
    assert parent['score'] == max(h['score'] for h in parent['hits'])
    assert {m['entity_type'] for m in search(entity_type='experiments')['matches']} == {'experiments'}
    graph.write_text('{', encoding='utf-8')
    broken = search()
    assert broken['warnings']
    assert broken['count'] == 3  # Keep unlinked evidence, don't invent provenance.


def test_search_cards_truncation_and_no_external_symlink(tmp_path, monkeypatch):
    import asyncio
    import os
    import pytest
    root = tmp_path / 'wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    (root / 'papers').mkdir(parents=True)
    (root / 'papers/a.md').write_text('# A\n## Method\n' + 'condition ' * 180)
    (root / 'papers/b.md').write_text('# B\n## Method\nLatest content.')
    ctx = ToolContext(tmp_path)
    first = json.loads(asyncio.run(invoke('search_wiki', ctx, query='condition',
                                         strategy='lexical', limit=1)).output)
    assert first['count'] == 1 and first['cards'][0]['title'] == 'A'
    field = first['cards'][0]['fields'][2]
    assert field['truncated'] and len(field['content']) == 700
    outside = tmp_path / 'secret.md'
    outside.write_text('OutsideSecret')
    try:
        os.symlink(outside, root / 'papers/escape.md')
    except OSError:
        pytest.skip('Host cannot create symlinks; remaining symlink check runs on Linux')
    result = asyncio.run(invoke('search_wiki', ctx, query='OutsideSecret'))
    assert json.loads(result.output)['count'] == 0


def test_agent_can_browse_search_then_read_limits_through_registered_tools(tmp_path, monkeypatch):
    from paper_agent.agent.model_client import ModelResponse, ToolCall
    root = tmp_path / 'shared-wiki'
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    page = root / 'papers/battery.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Battery\n## Method\n用松弛曲线预测容量。\n'
                    '## Limitations / Failure Modes\n需要给定未来工况。\n', encoding='utf-8')
    page.with_suffix('.source.json').write_text(json.dumps({
        'document_id': 'fixture-document', 'pdf_sha256': 'fixture-hash',
        'pdf_path': 'papers/source.pdf', 'evidence_path': 'research-wiki/sources/fixture.json',
        'source': {'kind': 'local_pdf'}, 'content_origin': 'paper_reported'}), encoding='utf-8')

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            schemas = {t['function']['name']: t['function'] for t in tools}
            assert 'search_wiki' in schemas
            assert not {'browse_wiki', 'read_wiki', 'expand_wiki_relations'} & schemas.keys()
            if self.calls == 1:
                return ModelResponse(tool_calls=(ToolCall('1', 'search_wiki',
                    {'query': '如何用松弛曲线预测容量'}),))
            payload = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
            card = payload['cards'][0]
            assert card['source_record']['document_id'] == 'fixture-document'
            assert any('需要给定未来工况' in field['content'] for field in card['fields'])
            assert card['quality_records'][0]['status'] == 'not_checked'
            return ModelResponse(content='工具链完成；此测试不评价模型选材能力。')

    agent = PhaseAgent(tmp_path, model_client=Model(), phase='novelty', max_iterations=4)
    assert '工具链完成' in agent.go('检查研究条件').final


def test_only_one_wiki_tool_is_registered_and_exposed_in_every_phase(tmp_path):
    from paper_agent.tools import builtin_tools, PHASE_TOOL_NAMES, tools_for_phase
    registered = {spec.name for spec in builtin_tools() if 'wiki' in spec.name}
    assert registered == {'search_wiki'}
    for phase in PHASE_TOOL_NAMES:
        names = {spec.name for spec in tools_for_phase(phase, builtin_tools())}
        assert names & registered == {'search_wiki'}
    import asyncio
    for name in ('read_wiki', 'browse_wiki', 'expand_wiki_relations', 'search_wiki_hybrid'):
        result = asyncio.run(invoke(name, ToolContext(tmp_path)))
        assert result.is_error and result.metadata['error_code'] == 'UNKNOWN_TOOL'


def test_single_query_reads_limits_and_neighbor_sources(tmp_path, monkeypatch):
    from paper_agent.tools.wiki import search_wiki
    root = tmp_path / 'wiki'
    (root / 'papers').mkdir(parents=True)
    (root / 'graph').mkdir()
    (root / 'papers/alpha.md').write_text(
        '# Alpha\n## Method\nRelaxation predicts capacity.\n'
        '## Limitations / Failure Modes\nRequires known future load.\n')
    (root / 'papers/beta.md').write_text(
        '# Beta\n## Method\nA separate related mechanism.\n')
    (root / 'graph/edges.jsonl').write_text(json.dumps({
        'from': 'paper:alpha', 'to': 'paper:beta', 'type': 'shares_method',
        'evidence': 'Synthetic method connection'}) + '\n')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    data = json.loads(search_wiki(ToolContext(tmp_path), 'Relaxation', strategy='lexical').output)
    assert {c['wiki_path'] for c in data['cards']} == {'papers/alpha.md', 'papers/beta.md'}
    assert any('Requires known future load.' in f['content']
               for c in data['cards'] for f in c['fields'])
    assert data['relation_expansions'] == 1
    assert data['relations'][0]['to'] == 'paper:beta'
    no_graph = json.loads(search_wiki(ToolContext(tmp_path), 'Relaxation',
                                     include_relations=False).output)
    assert no_graph['relation_expansions'] == 0 and no_graph['relations'] == []


def test_overview_uses_query_relevance_instead_of_filename_order(tmp_path, monkeypatch):
    from paper_agent.tools.wiki import search_wiki
    root = tmp_path / 'wiki'
    (root / 'papers').mkdir(parents=True)
    (root / 'papers/a.md').write_text('# Unrelated\n## Method\nProtein folding.\n')
    (root / 'papers/z.md').write_text('# Relevant\n## Method\nBattery relaxation.\n')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    data = json.loads(search_wiki(ToolContext(tmp_path), 'Battery relaxation',
                                 purpose='overview', limit=1).output)
    assert data['cards'][0]['wiki_path'] == 'papers/z.md'
    assert data['matches'][0]['wiki_path'] == 'papers/z.md'
