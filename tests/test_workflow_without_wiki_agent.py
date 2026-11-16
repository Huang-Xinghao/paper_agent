"""Wiki role restoration, final approval and existing research access."""

import json
from types import SimpleNamespace as NS
import pytest

from paper_agent.agent import coordinator, role_cli, workflow_cli
from paper_agent.agent.loop import PhaseAgent
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.agent.runtime.execution import ExecutionTask, StageAssignment
from paper_agent.agent.runtime.sessions import PhaseTask, RoleSession
from paper_agent.agent.terminal import main as main_terminal
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState
from paper_agent.storage import write_json


@pytest.fixture
def final_paper(tmp_path, monkeypatch):
    paper = tmp_path / 'paper'
    paper.mkdir()
    (paper / 'main.tex').write_text('Verified manuscript')
    (paper / 'main.pdf').write_bytes(b'%PDF-1.4\nfixture')
    (paper / 'PAPER_IMPROVEMENT_LOG.md').write_text('Both review rounds completed.')
    # Exercise actual approval/file freezing without invoking a LaTeX installation.
    monkeypatch.setattr(WorkflowRunner, '_compile_pdf', lambda self: paper / 'main.pdf')
    return tmp_path


def pending_final(project):
    state = WorkflowState(
        'final-review', project_goal='Research goal', mode='expert',
        current_node='improvement', status='awaiting_user',
        expert_checkpoint={
            'node': 'improvement', 'status': 'pending',
            'report': 'paper/PAPER_IMPROVEMENT_LOG.md', 'next_node': None,
        },
    )
    write_json(project / 'workflow-state.json', state.to_dict())


def test_cli_final_approval_dispatches_research_archival(final_paper, monkeypatch):
    pending_final(final_paper)
    calls = []
    class Main:
        def __init__(self, *args, **kwargs): pass
        def go(self, prompt, **kwargs):
            state = json.loads((final_paper / 'workflow-state.json').read_text())
            assert state['status'] == 'completed'
            assert state['expert_checkpoint']['status'] == 'approved'
            calls.append((prompt, kwargs))
            return NS(status='completed', final='已沉淀到 Wiki。', error=None)
    monkeypatch.setattr(coordinator, 'CoordinatorAgent', Main)
    result = workflow_cli.run_coordinated_workflow(final_paper, continue_run=True)
    assert result.status == 'completed'
    assert result.expert_checkpoint['status'] == 'approved'
    assert set(result.expert_checkpoint['confirmed_files']) == {
        'paper/PAPER_IMPROVEMENT_LOG.md', 'paper/main.tex', 'paper/main.pdf',
    }
    assert len(calls) == 1 and calls[0][1]['archive_completed']


def test_terminal_final_approval_dispatches_research_archival(final_paper, monkeypatch):
    pending_final(final_paper)
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'test:p1')
    calls = []
    class Main:
        def __init__(self, *args, **kwargs): pass
        def go(self, prompt, **kwargs):
            state = json.loads((final_paper / 'workflow-state.json').read_text())
            assert state['status'] == 'completed'
            assert state['expert_checkpoint']['status'] == 'approved'
            calls.append((prompt, kwargs))
            return NS(status='completed', final='已沉淀到 Wiki。', error=None)
    monkeypatch.setattr(coordinator, 'CoordinatorAgent', Main)
    folder = final_paper / '.paper-agent/main-terminal/turns/final-approval'
    write_json(folder / 'request.json', {
        'project': str(final_paper), 'prompt': '批准', 'options': {},
        'checkpoint': workflow_cli.checkpoint_snapshot(final_paper),
    })
    main_terminal.run_turn(folder)
    result = json.loads((folder / 'result.json').read_text())
    assert result['status'] == 'completed' and result['error'] is None
    state = json.loads((final_paper / 'workflow-state.json').read_text())
    assert state['expert_checkpoint']['status'] == 'approved'
    assert state['status'] == 'completed'
    assert len(calls) == 1 and calls[0][1]['archive_completed']


def test_main_accepts_final_stage_and_summarizes(final_paper, monkeypatch):
    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            names = {tool['function']['name'] for tool in tools}
            assert {'curate_wiki', 'archive_research'} <= names
            if self.calls == 1:
                return ModelResponse(tool_calls=(ToolCall('final', 'delegate_stage', {
                    'assignment': {'node': 'improvement', 'role': 'paper_writer',
                                   'instruction': 'Finish the final revision'},
                }),))
            if self.calls == 2:
                return ModelResponse(tool_calls=(ToolCall('archive', 'curate_wiki', {
                    'project_evidence': True,
                }),))
            return ModelResponse(content='最终修订已完成。')

    calls = []
    def finished(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get('kind') == 'wiki':
            return NS(status='completed', error=None, wiki_result={'status': 'completed'})
        return WorkflowState('finished', project_goal='Research goal',
                             current_node='improvement', status='completed')

    monkeypatch.setattr(coordinator, 'execute_stage', finished)
    model = Model()
    agent = coordinator.CoordinatorAgent(final_paper, model_client=model,
                                          project_goal='Research goal', stop_after='improvement')
    result = agent.go('Finish the final revision')
    assert model.calls == 3 and agent.assignments == 1
    assert calls[-1]["role"] == "wiki_agent" and calls[-1]["project_evidence"]
    assert result.status == 'completed' and result.final == '最终修订已完成。'
    assert not (final_paper / '.paper-agent/research-memory.json').exists()


def test_expert_summary_does_not_require_extra_review_tool(tmp_path, monkeypatch):
    report = tmp_path / 'literature/RESEARCH_REPORT.md'
    report.parent.mkdir()
    report.write_text('Current literature evidence')

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(tool_calls=(ToolCall('stage', 'delegate_stage', {
                    'assignment': {'node': 'literature', 'role': 'idea_generator',
                                   'instruction': 'Review literature'},
                }),))
            return ModelResponse(content='文献报告已生成，请审阅。')

    def pending(*args, **kwargs):
        return WorkflowState('pending', project_goal='Research goal', mode='expert',
            current_node='literature', status='awaiting_user', expert_checkpoint={
                'node': 'literature', 'status': 'pending',
                'report': 'literature/RESEARCH_REPORT.md', 'next_node': 'data_search',
            })

    monkeypatch.setattr(coordinator, 'execute_stage', pending)
    model = Model()
    agent = coordinator.CoordinatorAgent(tmp_path, model_client=model,
        project_goal='Research goal', mode='expert')
    result = agent.go('Review literature')
    assert model.calls == 2 and agent.assignments == 1
    assert result.status == 'awaiting_user'
    assert result.final == '文献报告已生成，请审阅。'
    with pytest.raises(ValueError, match='明确批准'):
        agent._approve_checkpoint(None)


@pytest.mark.parametrize('change', [
    {'role': 'wiki_agent'}, {'node': 'wiki'}, {'kind': 'wiki'},
])
def test_execution_contract_rejects_removed_role_node_and_kind(tmp_path, change):
    with pytest.raises(ValueError):
        ExecutionTask(id='a' * 32, project=str(tmp_path), instruction='Task',
                      project_goal='Goal', **{
                          'node': 'literature', 'role': 'idea_generator', 'kind': 'stage', **change,
                      })


def test_role_cli_supports_wiki_dispatch_and_independent_history(tmp_path):
    calls = []
    value = role_cli.run('wiki --paper example.pdf --project-evidence --source results.json',
                         project=tmp_path, curate=lambda task: calls.append(task) or {'status': 'completed'})
    assert value['status'] == 'completed'
    assert calls == [{'paper_path': 'example.pdf', 'project_evidence': True,
                      'source_paths': ['results.json'], 'instruction': '', 'force': False}]
    role_cli.run('history --role wiki_agent', project=tmp_path)
    with pytest.raises(ValueError):
        role_cli.run('delegate --node literature --role wiki_agent --instruction Task', project=tmp_path)


def test_phase_and_session_support_standalone_wiki_role(tmp_path):
    assert PhaseTask('wiki', 'Task').role == 'wiki_agent'
    assert RoleSession(tmp_path, 'wiki_agent').path.name == 'wiki_agent.json'
    with pytest.raises(ValueError):
        StageAssignment(node='literature', role='wiki_agent', instruction='Task')
    agent = PhaseAgent(tmp_path, phase='wiki', model_client=object())
    assert {'wiki_prepare', 'wiki_read_source', 'wiki_review', 'wiki_publish', 'wiki_abort'} <= {
        tool.name for tool in agent.tools}
    from paper_agent.agent.stages import WORKFLOW_NODE_NAMES
    assert 'wiki' not in WORKFLOW_NODE_NAMES


def test_existing_wiki_still_supplies_verified_evidence_without_publication(tmp_path, monkeypatch):
    from paper_agent.tools.core import ToolContext
    from paper_agent.tools.wiki import search_wiki, read_wiki

    wiki = tmp_path / 'wiki'
    page = wiki / 'papers/example.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Example\n## Method\nRelaxation predicts capacity.\n')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(wiki))
    before = {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob('*') if p.is_file()}
    result = search_wiki(ToolContext(tmp_path / 'project'), 'Relaxation',
                         strategy='lexical', include_relations=False)
    assert not result.is_error
    match = json.loads(result.output)['matches'][0]
    result = read_wiki(ToolContext(tmp_path / 'project'), match['wiki_path'])
    assert not result.is_error
    card = json.loads(result.output)
    assert card['wiki_path'] == 'papers/example.md'
    assert card['content_sha256']
    assert card['content'] == page.read_text()
    assert card['content_complete']
    assert before == {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob('*') if p.is_file()}


def test_main_final_checkpoint_approval_requires_wiki_handoff(final_paper, monkeypatch):
    pending_final(final_paper)
    calls = []
    def dispatched(*args, **kwargs):
        calls.append(kwargs)
        assert json.loads((final_paper / 'workflow-state.json').read_text())['status'] == 'completed'
        return NS(status='completed', error=None, wiki_result={'status': 'completed'})
    monkeypatch.setattr(coordinator, 'execute_stage', dispatched)
    class Model:
        n = 0
        def complete(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return ModelResponse(tool_calls=(ToolCall('approve', 'approve_checkpoint', {}),))
            if self.n == 2:
                return ModelResponse(tool_calls=(ToolCall('archive', 'curate_wiki', {'project_evidence': True}),))
            return ModelResponse(content='已批准并沉淀研究成果。')
    model = Model()
    result = coordinator.CoordinatorAgent(final_paper, model_client=model).go('批准')
    assert result.status == 'completed' and len(calls) == 1
    assert calls[0]['role'] == 'wiki_agent' and calls[0]['project_evidence']
