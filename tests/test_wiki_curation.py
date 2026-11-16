"""Exercise curation through the real role loop and publisher with local model/parser fixtures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from paper_agent.agent import coordinator
from paper_agent.agent.runtime import execution
from paper_agent.agent.runtime.sessions import RoleSession
from paper_agent.agent.wiki_role import run_wiki_role
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.storage import write_json
from paper_agent.tools import wiki_ingest
from paper_agent.tools.core import ToolContext
from paper_agent.tools.paper_preprocessing import document_from_structured
from paper_agent.tools.research_wiki import CLASSIFICATION_FIELDS
from paper_agent.tools.wiki import search_wiki


@pytest.fixture
def curation(tmp_path, monkeypatch):
    project, root = tmp_path / 'project', tmp_path / 'wiki'
    project.mkdir()
    pdf = root / 'raw-paper/source.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'%PDF-1.4\nfixture')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(root))
    text = 'A linear filter estimates capacity from voltage. A controlled simulation failed on noisy cells. No named dataset was used.'
    document = document_from_structured({
        'pages': [{'page_idx': 0, 'blocks': [{'type': 'text', 'content': text}]}],
    }, pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    monkeypatch.setattr(wiki_ingest, 'read_mineru_paper', lambda *a, **kw: {
        'document': deepcopy(document), 'figures': [], 'cache': str(project / 'cache'),
    })
    labels = {name: [] for name in CLASSIFICATION_FIELDS}
    sections = {'Abstract': text, 'One-line thesis': 'A linear filter estimates capacity from voltage.',
                'Dataset and Features': 'No named dataset was used.'}
    evidence = [{'block_id': document['blocks'][0]['id'], 'quote': text}]
    draft = {
        'metadata': {'title': 'Linear capacity filtering', 'authors': ['Example Author'],
                     'year': 2026, 'venue': 'Example Journal', **deepcopy(labels)},
        'sections': sections,
        'section_status': {name: {'status': 'extracted' if name != 'Dataset and Features' else 'not_applicable',
                                  'origin': 'author_reported', 'reason': 'Reported in source.',
                                  'evidence': evidence} for name in sections},
        'critical_claims': [{'id': 'result', 'kind': 'main_result', 'section': 'One-line thesis',
                             'statement': sections['One-line thesis'], 'evidence': evidence}],
        'idea': {'title': 'Linear capacity filtering', 'thesis': sections['One-line thesis'],
                 'structure': '```text\nvoltage -> linear filter -> capacity\n```\nVoltage is the input; capacity is the output.',
                 **deepcopy(labels)},
        'experiments': [{'title': 'Noisy-cell simulation', 'question': 'Does filtering tolerate noisy cells?',
                         'hypothesis': 'Filtering tolerates noise.', 'protocol': 'Controlled simulation.',
                         'results': 'The simulation failed on noisy cells.', **deepcopy(labels)}], 'datasets': [], 'references': [], 'coined_terms': [], 'figure_selection': [],
    }
    return NS(project=project, root=root, pdf=pdf, document=document, draft=draft)


class CurationModel:
    """Role authors a draft; a separate tool-free request reviews it against sources."""
    def __init__(self, fixture, project_evidence=False):
        self.fixture, self.project_evidence = fixture, project_evidence
        self.step, self.reviews = 0, 0

    def complete(self, messages, tools):
        if not tools:
            self.reviews += 1
            payload = json.loads(messages[-1]['content'])
            draft = payload['draft']
            assert payload['source_document']['blocks']
            verdict = {'status': 'supported', 'reason': 'Consistent with the supplied source.'}
            review = {'claims': [{'id': c['id'], **verdict} for c in draft['critical_claims']],
                      'sections': {s: verdict for s in draft['sections']}, 'entities': verdict,
                      'coverage': {**verdict, 'missing': []}, 'issues': []}
            return ModelResponse(content=json.dumps(review))
        self.step += 1
        if self.step > 1 and self.step != 4:
            result = json.loads(messages[-1]['content'])
            assert not result.get('error'), result
        if self.step == 1:
            assert 'Wiki' in messages[0]['content'] and 'wiki-curation' in messages[0]['content']
            name, args = 'wiki_prepare', {'paper_path': str(self.fixture.pdf),
                                         'project_evidence': self.project_evidence}
        elif self.step == 2:
            if result.get('status') == 'skipped':
                return ModelResponse(content='Unchanged Wiki input skipped.')
            self.draft_path = result['draft_path']
            name, args = 'wiki_read_source', {}
        elif self.step == 3:
            assert result['next_offset'] is None
            name, args = 'write', {'path': self.draft_path, 'content': json.dumps(self.fixture.draft)}
        elif self.step == 4:
            name, args = 'wiki_review', {'draft_path': self.draft_path}
        elif self.step == 5:
            assert result['publishable']
            name, args = 'wiki_publish', {'draft_path': self.draft_path}
        else:
            assert result['status'] == 'completed'
            return ModelResponse(content='Published verified Wiki pages.')
        return ModelResponse(tool_calls=(ToolCall(str(self.step), name, args),))


@pytest.mark.parametrize('project_evidence', [False, True])
def test_pdf_to_searchable_wiki_and_unchanged_input_skip(curation, project_evidence):
    f = curation
    if project_evidence:
        write_json(f.project / 'results/metrics.json', {'failed_trials': 2})
        report = f.project / 'refine-logs/EXPERIMENT_RESULTS.md'
        report.parent.mkdir()
        report.write_text('Negative trials are recorded in results/metrics.json.')
    model = CurationModel(f, project_evidence)
    options = dict(paper_path=str(f.pdf), project_evidence=project_evidence, complete_relations=False)
    result = run_wiki_role(f.project, 'Curate the supplied paper.', model_client=model, **options)
    assert result['status'] == 'completed' and model.reviews == 1
    assert Path(result['wiki_path']).is_file() and Path(result['idea_path']).is_file()
    assert len(result['experiment_paths']) == 1
    assert 'failed on noisy cells' in Path(result['experiment_paths'][0]).read_text()
    assert Path(f.project / result['receipt_path']).is_file()
    if project_evidence:
        saved = json.loads((f.project / '.paper-agent/research-memory.json').read_text())
        assert 'results/metrics.json' in saved['inputs']
        assert saved['research_id']
    found = search_wiki(ToolContext(f.project), 'linear filter', strategy='lexical', include_relations=False)
    assert not found.is_error
    assert json.loads(found.output)['matches']
    session = RoleSession(f.project, 'wiki_agent').load()
    assert session['tasks'][-1]['status'] == 'completed'
    assert session['tasks'][-1]['phase'] == 'wiki'
    assert not RoleSession(f.project, 'idea_generator').path.exists()
    before = Path(result['wiki_path']).read_bytes()
    second = CurationModel(f, project_evidence)
    repeated = run_wiki_role(f.project, 'Curate again.', model_client=second, **options)
    assert repeated['status'] == 'skipped' and second.reviews == 0
    assert Path(result['wiki_path']).read_bytes() == before


def prepared(f):
    ingestion = wiki_ingest.WikiIngestion(f.project, model=CurationModel(f))
    ingestion.assign(paper_path=str(f.pdf), complete_relations=False)
    ctx = ToolContext(f.project)
    ingestion.wiki_prepare(ctx, str(f.pdf))
    write_json(ingestion.draft_path, f.draft)
    return ingestion, ctx


def test_publish_requires_full_read_and_current_independent_approval(curation):
    f = curation
    ingestion, ctx = prepared(f)
    with pytest.raises(ValueError, match='complete prepared source'):
        ingestion.wiki_review(ctx, str(ingestion.draft_path))
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    altered = deepcopy(f.draft)
    altered['idea']['thesis'] += ' Changed.'
    write_json(ingestion.draft_path, altered)
    with pytest.raises(ValueError, match='exact draft'):
        ingestion.wiki_publish(ctx, str(ingestion.draft_path))
    assert not (f.root / 'papers').exists()


def test_source_change_invalidates_publication(curation):
    f = curation
    ingestion, ctx = prepared(f)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    f.pdf.write_bytes(b'%PDF modified')
    with pytest.raises(ValueError, match='Source changed'):
        ingestion.wiki_publish(ctx, str(ingestion.draft_path))
    assert ingestion.approval is None


def test_wiki_writes_are_staged_and_abort_saves_failure(curation):
    ingestion, ctx = prepared(curation)
    with pytest.raises(ValueError, match='limited to draft'):
        ingestion.validate_draft_write('literature/RESEARCH_REPORT.md')
    ingestion.wiki_abort(ctx, 'Parser could not supply the required evidence.')
    ingestion.check_completion()
    assert json.loads(ingestion.receipt_path.read_text())['status'] == 'failed'
    assert curation.pdf.is_file()


def test_shared_library_task_uses_unified_executor_and_preserves_workflow(curation, monkeypatch):
    from paper_agent.agent import wiki_role
    f = curation
    original = wiki_role.run_wiki_role
    monkeypatch.setattr(wiki_role, 'run_wiki_role', lambda *a, **kw: original(
        *a, model_client=CurationModel(f), **kw))
    write_json(f.project / 'workflow-state.json', {'run_id': 'existing', 'status': 'completed', 'current_node': 'improvement'})
    before = (f.project / 'workflow-state.json').read_bytes()
    result = execution.dispatch_stage(f.project, 'wiki', 'Curate PDF.', role='wiki_agent',
        kind='wiki', project_goal='', paper_path=str(f.pdf), complete_relations=False)
    assert result.status == 'completed' and result.wiki_result['status'] == 'completed'
    assert result.execution['backend'] == 'local'
    assert (f.project / 'workflow-state.json').read_bytes() == before
    with pytest.raises(RuntimeError, match='already started'):
        from paper_agent.agent.runtime.herdr import directory
        execution.execute_task(directory(f.project) / 'tasks' / result.task_id)


def test_main_can_dispatch_single_pdf_without_research_goal(curation, monkeypatch):
    f = curation
    calls = []
    def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return NS(status='completed', error=None, wiki_result={'status': 'completed'})
    monkeypatch.setattr(coordinator, 'execute_stage', dispatch)
    class Model:
        n = 0
        def complete(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return ModelResponse(tool_calls=(ToolCall('curate', 'role_cli', {
                    'command': 'wiki --paper ' + str(f.pdf)}),))
            return ModelResponse(content='PDF curated.')
    main = coordinator.CoordinatorAgent(f.project, model_client=Model())
    assert main.go('将这篇论文整理入库').status == 'completed'
    assert main.assignments == 1 and not main.project_goal
    assert calls[0]['role'] == 'wiki_agent' and calls[0]['kind'] == 'wiki'
    assert not (f.project / 'workflow-state.json').exists()


def test_archival_failure_preserves_completed_research(curation, monkeypatch):
    f = curation
    def fail(*args, **kwargs): raise RuntimeError('Parser unavailable')
    monkeypatch.setattr(coordinator, 'execute_stage', fail)
    # Use a project-local manuscript for the default research completion path.
    paper = f.project / 'paper/main.pdf'
    paper.parent.mkdir()
    paper.write_bytes(f.pdf.read_bytes())
    class Model:
        n = 0
        def complete(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return ModelResponse(tool_calls=(ToolCall('archive', 'archive_research', {}),))
            assert 'Parser unavailable' in messages[-1]['content']
            return ModelResponse(content='研究完成，Wiki 入库失败，可单独重试。')
    result = coordinator.CoordinatorAgent(f.project, model_client=Model()).go(
        '沉淀成果', archive_completed=True)
    assert result.status == 'completed' and paper.is_file()
    assert '失败' in result.final


def test_updated_research_reuses_identity_and_archives_superseded_experiments(curation):
    f = curation
    report = f.project / 'refine-logs/EXPERIMENT_RESULTS.md'
    report.parent.mkdir()
    report.write_text('A controlled simulation failed on noisy cells.')
    options = dict(paper_path=str(f.pdf), project_evidence=True, complete_relations=False)
    first = run_wiki_role(f.project, 'Publish research.', model_client=CurationModel(f, True), **options)
    report.write_text('A controlled simulation failed on noisy cells. Retain this as a limitation.')
    f.draft['experiments'] = []
    second = run_wiki_role(f.project, 'Update research.', model_client=CurationModel(f, True), **options)
    assert second['status'] == 'completed'
    assert first['research_id'] == second['research_id'] and first['wiki_path'] == second['wiki_path']
    assert len(list((f.root / 'papers').glob('*.md'))) == 1
    assert not Path(first['experiment_paths'][0]).exists()
    assert list((f.root / 'archive/autoresearch').rglob('*.md'))


def test_completion_cannot_skip_archival_and_scoped_tasks_cannot_publish(curation):
    f = curation
    main = coordinator.CoordinatorAgent(f.project, model_client=object())
    main._archive_due = True
    from paper_agent.agent.loop import ArtifactValidationError
    with pytest.raises(ArtifactValidationError, match='curate_wiki'):
        main._complete_research()
    scoped = coordinator.CoordinatorAgent(f.project, model_client=object(), stage_task={
        'node': 'literature', 'role': 'idea_generator', 'instruction': 'Read literature.'})
    with pytest.raises(ValueError, match='scoped'):
        scoped._curate_wiki(None, paper_path=str(f.pdf))
    assert scoped.assignments == 0


def test_web_role_snapshot_reports_wiki_execution(curation):
    from paper_agent.api.agents import agent_views
    values = agent_views(curation.project, 'running', {}, [], last_event={
        'kind': 'role_task_start', 'data': {'role': 'wiki_agent', 'phase': 'wiki'}})
    assert values[0]['role'] == 'main'
    roles = {v['role']: v for v in values}
    assert roles['wiki_agent']['is_active'] and roles['wiki_agent']['status'] == 'running'
    assert roles['main']['status'] == 'waiting'


def test_restart_restores_read_cursor_and_approval_without_parser_or_review(curation, monkeypatch):
    f = curation
    ingestion, ctx = prepared(f)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    original_job = ingestion.job

    def unexpected(*args, **kwargs):
        raise AssertionError('Resume must not parse or request independent review again')

    monkeypatch.setattr(wiki_ingest, 'read_mineru_paper', unexpected)
    model = NS(complete=unexpected)
    resumed = wiki_ingest.WikiIngestion(f.project, model=model)
    resumed.assign(paper_path=str(f.pdf), complete_relations=False)
    result = json.loads(resumed.wiki_prepare(ctx, str(f.pdf)).output)
    assert result['resumed'] and result['next_offset'] is None and result['publishable']
    assert resumed.job == original_job
    assert resumed.review_count == 1
    assert not resumed.wiki_review(ctx, str(resumed.draft_path)).is_error
    assert resumed.review_count == 1
    assert json.loads(resumed.wiki_publish(ctx, str(resumed.draft_path)).output)['status'] == 'completed'


def test_restart_keeps_original_project_sources_and_partial_cursor(curation):
    f = curation
    evidence = f.project / 'paper/PAPER_IMPROVEMENT_LOG.md'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('Two independent reviews recorded here.')
    ctx = ToolContext(f.project)
    ingestion = wiki_ingest.WikiIngestion(f.project, model=CurationModel(f))
    ingestion.assign(paper_path=str(f.pdf), project_evidence=True,
                     source_paths=['paper/PAPER_IMPROVEMENT_LOG.md'])
    ingestion.wiki_prepare(ctx, str(f.pdf), project_evidence=True)
    ingestion.wiki_read_source(ctx, limit=100)
    resumed = wiki_ingest.WikiIngestion(f.project, model=CurationModel(f))
    resumed.assign(paper_path=str(f.pdf), project_evidence=True)
    result = json.loads(resumed.wiki_prepare(ctx, str(f.pdf), project_evidence=True).output)
    assert result['next_offset'] == 100
    assert result['document_id'] == ingestion.document['document_id']
    assert 'paper/PAPER_IMPROVEMENT_LOG.md' in resumed.inputs
    assert resumed.job == ingestion.job


def test_restart_changed_evidence_prepares_new_version_and_invalidates_approval(curation):
    f = curation
    ctx = ToolContext(f.project)
    evidence = f.project / 'evidence.md'
    evidence.write_text('Original evidence')
    ingestion = wiki_ingest.WikiIngestion(f.project, model=CurationModel(f))
    ingestion.assign(paper_path=str(f.pdf), project_evidence=True, source_paths=['evidence.md'])
    ingestion.wiki_prepare(ctx, str(f.pdf), project_evidence=True)
    evidence.write_text('Changed evidence')
    resumed = wiki_ingest.WikiIngestion(f.project, model=CurationModel(f))
    resumed.assign(paper_path=str(f.pdf), project_evidence=True)
    result = json.loads(resumed.wiki_prepare(ctx, str(f.pdf), project_evidence=True).output)
    assert not result.get('resumed') and resumed.job != ingestion.job
    assert resumed.document['document_id'] != ingestion.document['document_id']
    assert 'evidence.md' in resumed.inputs
    assert resumed.approval is None and resumed.read_ranges == []


def test_restart_draft_edit_requires_new_review(curation):
    ingestion, ctx = prepared(curation)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    resumed = wiki_ingest.WikiIngestion(curation.project, model=CurationModel(curation))
    resumed.assign(paper_path=str(curation.pdf), complete_relations=False)
    altered = deepcopy(curation.draft)
    altered['idea']['thesis'] += ' Changed.'
    write_json(resumed.draft_path, altered)
    with pytest.raises(ValueError, match='exact draft'):
        resumed.wiki_publish(ctx, str(resumed.draft_path))
    resumed.wiki_review(ctx, str(resumed.draft_path))
    assert resumed.review_count == 2


def test_legacy_draft_adopts_original_sources_and_completed_review(curation):
    f = curation
    ingestion, ctx = prepared(f)
    ingestion.wiki_read_source(ctx)
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    # Simulate a job written before state checkpoints were introduced.
    (ingestion.job / 'state.json').unlink()
    ingestion._state_pointer(f.pdf, False).unlink()
    model = CurationModel(f)
    resumed = wiki_ingest.WikiIngestion(f.project, model=model)
    resumed.assign(paper_path=str(f.pdf), complete_relations=False)
    result = json.loads(resumed.wiki_prepare(ctx, str(f.pdf)).output)
    assert result['resumed'] and result['publishable']
    assert result['next_offset'] is None
    assert resumed.job == ingestion.job
    assert not resumed.wiki_review(ctx, str(resumed.draft_path)).is_error
    assert model.reviews == 0


def test_review_format_failure_does_not_spend_revision_budget(curation):
    ingestion, ctx = prepared(curation)
    ingestion.wiki_read_source(ctx)
    good_model = ingestion.model
    ingestion.model = NS(complete=lambda *args: ModelResponse(content='invalid JSON'))
    for _ in range(4):
        with pytest.raises(RuntimeError, match='Wiki evidence request failed'):
            ingestion.wiki_review(ctx, str(ingestion.draft_path))
        assert ingestion.review_count == 0
        state = json.loads((ingestion.job / 'state.json').read_text())
        assert state['review_count'] == 0
        assert ingestion.approval is None
    ingestion.model = good_model
    assert not ingestion.wiki_review(ctx, str(ingestion.draft_path)).is_error
    assert ingestion.review_count == 1
