"""Wiki synthesis must not silently expand into a raw PDF review."""
import json

import pytest

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState
from paper_agent.tools.core import ToolContext, read, write
from paper_agent.tools import wiki as wiki_tools
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.tools.wiki_retrieval import load_page


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    root = tmp_path / 'wiki'
    page = root / 'papers/battery.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Battery lifetime\n## Method\nSplit by cell to test unseen cells.\n')
    raw = root / 'raw-paper'
    raw.mkdir()
    (raw / 'battery-lifetime.pdf').write_bytes(b'%PDF fixture must not be opened')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    monkeypatch.delenv('PAPER_AGENT_EMBEDDING_DIR', raising=False)
    monkeypatch.delenv('PAPER_AGENT_EMBEDDING_PYTHON', raising=False)
    return root


def test_search_then_read_wiki_from_independent_project(tmp_path, wiki):
    project = tmp_path / 'independent-project'
    project.mkdir()
    ctx = ToolContext(project)
    result = wiki_tools.search_wiki(ctx, query='battery lifetime', strategy='lexical',
                                    include_relations=False)
    match = json.loads(result.output)['matches'][0]
    assert 'content' not in match
    result = wiki_tools.read_wiki(ctx, match['wiki_path'])
    assert not result.is_error
    card = json.loads(result.output)
    assert card['wiki_path'] == 'papers/battery.md'
    assert card['read_path'] == str(wiki / 'papers/battery.md')
    assert card['content'] == (wiki / 'papers/battery.md').read_text()
    assert card['content_complete']
    content = read(ctx, card['read_path'])
    assert not content.is_error
    assert 'Split by cell to test unseen cells.' in content.output
    before = (wiki / 'papers/battery.md').read_bytes()
    with pytest.raises(ValueError, match='escapes workspace'):
        write(ctx, card['read_path'], 'Must not overwrite shared evidence')
    assert (wiki / 'papers/battery.md').read_bytes() == before


@pytest.mark.parametrize('escape', ['absolute', 'parent', 'symlink', 'directory_symlink'])
def test_shared_wiki_markdown_cannot_escape_root(tmp_path, wiki, escape):
    project = tmp_path / 'independent-project'
    project.mkdir()
    outside = tmp_path / 'private.md'
    outside.write_text('Private evidence outside shared Wiki')
    if escape == 'absolute':
        path = outside
    elif escape == 'parent':
        path = wiki / 'papers/../../private.md'
    elif escape == 'symlink':
        path = wiki / 'papers/escape.md'
        path.symlink_to(outside)
    else:
        (wiki / 'linked').symlink_to(tmp_path, target_is_directory=True)
        path = wiki / 'linked/private.md'
    with pytest.raises(ValueError, match='outside workspace'):
        read(ToolContext(project), str(path))


@pytest.mark.parametrize('phase,output', [
    ('literature', 'literature/RESEARCH_REPORT.md'),
    ('ideas', 'idea-stage/IDEA_REPORT.md'),
    ('research_refine', 'refine-logs/FINAL_PROPOSAL.md'),
])
def test_role_owns_wiki_research_without_workflow_model_calls(tmp_path, monkeypatch, wiki, phase, output):
    pdf_reads = []
    def forbid_pdf(*args, **kwargs):
        pdf_reads.append(args)
        raise AssertionError('Wiki screening must not open a PDF')
    monkeypatch.setattr('pypdf.PdfReader', forbid_pdf)

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                names = {tool['function']['name'] for tool in tools}
                assert {'search_wiki', 'read_wiki', 'read_paper_pages'} <= names
                system = messages[0]['content']
                assert 'Wiki' in system and '## Skill:' in system
                assert messages[-1]['content'].startswith('Current assignment')
                assert 'Wiki 取证包' not in messages[-1]['content']
                assert '运行时文献使用规则' not in system
                assert 'First check the battery resource library' not in system
                return ModelResponse(tool_calls=(ToolCall('wiki', 'search_wiki', {
                    'query': 'battery lifetime', 'strategy': 'lexical', 'include_relations': False,
                }),))
            if self.calls == 2:
                result = json.loads(messages[-1]['content'])
                assert result['view'] == 'titles'
                return ModelResponse(tool_calls=(ToolCall('page', 'read_wiki', {
                    'wiki_path': result['matches'][0]['wiki_path'],
                }),))
            if self.calls == 3:
                result = json.loads(messages[-1]['content'])
                assert 'Split by cell' in result['content']
                return ModelResponse(tool_calls=(ToolCall('report', 'write', {
                    'path': output,
                    'content': 'Wiki evidence: papers/battery.md, Method. '
                               'Split by cell to test unseen cells. PDF not verified in this run.',
                }),))
            return ModelResponse(content='Wiki 文献报告已完成。')

    project = tmp_path / 'project'
    for relative in ('literature/RESEARCH_REPORT.md', 'data-search/DATA_REPORT.md',
                     'idea-stage/IDEA_REPORT.md'):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Existing research evidence')
    events = []
    agent = PhaseAgent(project, model_client=Model(), phase=phase,
                       assignment_role='idea_generator',
                       on_progress=lambda kind, data: events.append((kind, data)))
    runner = WorkflowRunner(agent)
    handler = {'literature': runner._run_literature, 'ideas': runner._run_ideas,
               'research_refine': runner._run_refine}[phase]
    result = handler(WorkflowState('wiki-first'), 'battery lifetime')
    assert result.artifact == output
    assert not (project / 'literature/SOURCE_CONTEXT.json').exists()
    assert not (project / '.paper-agent/wiki-retrieval').exists()
    assert pdf_reads == []
    assert agent.model.calls == 4
    searches = [data for kind, data in events
                if kind == 'tool_start' and data.get('tool') == 'search_wiki']
    assert len(searches) == 1
    assert searches[0]['role'] == 'idea_generator' and searches[0]['phase'] == phase


def test_search_titles_does_not_deliver_abstracts_bodies_or_archives(wiki, monkeypatch):
    path = wiki / 'papers/battery.md'
    path.write_text('# Battery lifetime\n## Abstract\nABSTRACT_ONLY\n## Method\nBODY_ONLY\n')
    monkeypatch.setattr(wiki_tools, '_quality_records', lambda *args: [{
        'status': 'needs_review', 'issues': ['ARCHIVE_ONLY' * 10000],
    }])
    result = wiki_tools.search_wiki(ToolContext(wiki.parent), 'battery lifetime',
                                   strategy='lexical', include_relations=False)
    assert not result.is_error
    delivered = json.loads(result.output)
    assert delivered['view'] == 'titles' and delivered['candidate_only']
    assert delivered['returned_count'] == 1
    assert delivered['matches'][0]['title'] == 'Battery lifetime'
    assert delivered['matches'][0]['wiki_path'] == 'papers/battery.md'
    for hidden in ('ABSTRACT_ONLY', 'BODY_ONLY', 'ARCHIVE_ONLY'):
        assert hidden not in result.output
    assert 'cards' not in delivered


@pytest.mark.parametrize('heading', ['Abstract', '摘要'])
def test_search_returns_complete_existing_abstract_only(wiki, heading):
    abstract = 'Existing summary. ' * 100
    path = wiki / 'papers/battery.md'
    path.write_text(f'# Battery lifetime\n## {heading}\n{abstract}\n## Method\nBODY_ONLY\n')
    result = wiki_tools.search_wiki(ToolContext(wiki.parent), 'battery lifetime',
                                   view='abstracts', strategy='lexical', include_relations=False)
    assert not result.is_error
    match = json.loads(result.output)['matches'][0]
    assert match['abstract_status'] == 'present'
    assert match['abstract'] == abstract.strip()
    assert 'BODY_ONLY' not in result.output


@pytest.mark.parametrize('abstract_section', ['', '## Abstract\n_TODO: add summary\n'])
def test_missing_abstract_is_not_synthesized_from_method(wiki, abstract_section):
    (wiki / 'papers/battery.md').write_text(
        f'# Battery lifetime\n{abstract_section}## Method\nDO_NOT_SUMMARIZE\n')
    result = wiki_tools.search_wiki(ToolContext(wiki.parent), 'battery lifetime',
                                   view='abstracts', strategy='lexical', include_relations=False)
    match = json.loads(result.output)['matches'][0]
    assert match['abstract_status'] == 'missing' and match['abstract'] == ''
    assert 'DO_NOT_SUMMARIZE' not in result.output


def test_read_wiki_delivers_all_sections_and_large_markdown(tmp_path, wiki):
    path = wiki / 'papers/battery.md'
    text = ('---\nsource: fixture\n---\n# Battery lifetime\n## Method\n'
            + 'Full method with cell-level split. ' * 2000
            + '\n## 自定义研究结果\nA result outside the old card headings.\n'
            + '## References\nFinal reference, previously omitted.\n')
    path.write_text(text)
    result = wiki_tools.read_wiki(ToolContext(tmp_path / 'project'), 'papers/battery.md')
    assert not result.is_error
    delivered = json.loads(result.output)
    assert delivered['content'] == text and len(text) > 60000
    assert delivered['content_complete']
    assert delivered['content_sha256'] == load_page(path).fingerprint
    assert delivered['wiki_path'] == 'papers/battery.md'
    assert 'quality_records' in delivered and 'source_record' in delivered


@pytest.mark.parametrize('escape', ['absolute', 'parent', 'symlink', 'directory_symlink', 'archive'])
def test_read_wiki_rejects_paths_outside_live_entity_pages(tmp_path, wiki, escape):
    outside = tmp_path / 'private.md'
    outside.write_text('PRIVATE_CONTENT')
    if escape == 'absolute':
        path = str(wiki / 'papers/battery.md')
    elif escape == 'parent':
        path = 'papers/../../private.md'
    elif escape == 'symlink':
        (wiki / 'papers/escape.md').symlink_to(outside)
        path = 'papers/escape.md'
    elif escape == 'directory_symlink':
        (wiki / 'linked').symlink_to(tmp_path, target_is_directory=True)
        path = 'linked/private.md'
    else:
        (wiki / 'archive').mkdir()
        (wiki / 'archive/private.md').write_text('PRIVATE_CONTENT')
        path = 'archive/private.md'
    result = wiki_tools.read_wiki(ToolContext(tmp_path / 'project'), path)
    assert result.is_error and 'PRIVATE_CONTENT' not in result.output


def test_invalid_view_and_missing_page_are_clear_errors(wiki):
    ctx = ToolContext(wiki.parent)
    assert wiki_tools.search_wiki(ctx, 'battery', view='full').is_error
    result = wiki_tools.read_wiki(ctx, 'papers/missing.md')
    assert result.is_error and 'not found' in result.output


def test_graph_discovery_returns_candidates_without_neighbor_bodies(wiki):
    neighbor = wiki / 'papers/neighbor.md'
    neighbor.write_text('# Related method\n## Abstract\nNeighbor abstract.\n## Method\nHIDDEN_BODY\n')
    graph = wiki / 'graph/edges.jsonl'
    graph.parent.mkdir()
    graph.write_text(json.dumps({
        'from': 'paper:battery', 'to': 'paper:neighbor', 'type': 'cites',
        'evidence': 'HIDDEN_GRAPH_PASSAGE',
    }) + '\n')
    result = wiki_tools.search_wiki(ToolContext(wiki.parent), 'battery lifetime',
                                   view='abstracts', strategy='lexical')
    assert not result.is_error
    delivered = json.loads(result.output)
    related = next(m for m in delivered['matches'] if m['wiki_path'] == 'papers/neighbor.md')
    assert related['origin'] == 'graph' and related['abstract'] == 'Neighbor abstract.'
    assert delivered['relations'][0]['relation'] == 'cites'
    assert 'HIDDEN_BODY' not in result.output and 'HIDDEN_GRAPH_PASSAGE' not in result.output


def test_wiki_read_available_in_all_search_phases():
    from paper_agent.tools import builtin_tools, tools_for_phase, PHASE_TOOL_NAMES
    for phase, names in PHASE_TOOL_NAMES.items():
        if 'search_wiki' in names:
            assert 'read_wiki' in {spec.name for spec in tools_for_phase(phase, builtin_tools())}


@pytest.mark.parametrize('phase', ['literature', 'ideas', 'research_refine'])
def test_highly_relevant_papers_can_still_be_checked_at_specific_pages(tmp_path, phase):
    agent = PhaseAgent(tmp_path, model_client=object(), phase=phase)
    assert 'read_paper_pages' in {spec.name for spec in agent.tools}
