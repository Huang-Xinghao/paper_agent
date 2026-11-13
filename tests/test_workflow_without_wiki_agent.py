"""Workflow completion and research access after removing WikiAgent."""

import json
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


def test_cli_final_approval_completes_without_another_agent(final_paper, monkeypatch):
    pending_final(final_paper)
    monkeypatch.setattr(coordinator, 'CoordinatorAgent',
                        lambda *a, **kw: pytest.fail('Final approval must not launch another agent'))
    result = workflow_cli.run_coordinated_workflow(final_paper, continue_run=True)
    assert result.status == 'completed'
    assert result.expert_checkpoint['status'] == 'approved'
    assert set(result.expert_checkpoint['confirmed_files']) == {
        'paper/PAPER_IMPROVEMENT_LOG.md', 'paper/main.tex', 'paper/main.pdf',
    }
    assert not (final_paper / '.paper-agent/research-memory.json').exists()


def test_terminal_final_approval_completes_without_another_agent(final_paper, monkeypatch):
    pending_final(final_paper)
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'test:p1')
    monkeypatch.setattr(coordinator, 'CoordinatorAgent',
                        lambda *a, **kw: pytest.fail('Final approval must not launch another agent'))
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
    assert not (final_paper / '.paper-agent/research-memory.json').exists()


def test_main_accepts_final_stage_and_summarizes(final_paper, monkeypatch):
    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            names = {tool['function']['name'] for tool in tools}
            assert 'curate_wiki' not in names and 'archive_research' not in names
            if self.calls == 1:
                return ModelResponse(tool_calls=(ToolCall('final', 'delegate_stage', {
                    'assignment': {'node': 'improvement', 'role': 'paper_writer',
                                   'instruction': 'Finish the final revision'},
                }),))
            return ModelResponse(content='最终修订已完成。')

    def finished(*args, **kwargs):
        return WorkflowState('finished', project_goal='Research goal',
                             current_node='improvement', status='completed')

    monkeypatch.setattr(coordinator, 'execute_stage', finished)
    model = Model()
    agent = coordinator.CoordinatorAgent(final_paper, model_client=model,
                                          project_goal='Research goal', stop_after='improvement')
    result = agent.go('Finish the final revision')
    assert model.calls == 2 and agent.assignments == 1
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


@pytest.mark.parametrize('command', [
    'wiki --paper example.pdf',
    'history --role wiki_agent',
    'delegate --node literature --role wiki_agent --instruction Task',
])
def test_role_cli_rejects_wiki_requests(tmp_path, command):
    with pytest.raises(ValueError):
        role_cli.run(command, project=tmp_path)


def test_phase_and_session_reject_removed_role(tmp_path):
    with pytest.raises(ValueError):
        PhaseTask('wiki', 'Task')
    with pytest.raises(ValueError):
        RoleSession(tmp_path, 'wiki_agent')
    with pytest.raises(ValueError):
        StageAssignment(node='literature', role='wiki_agent', instruction='Task')
    with pytest.raises(ValueError):
        PhaseAgent(tmp_path, phase='wiki')


def test_existing_wiki_still_supplies_verified_evidence_without_publication(tmp_path, monkeypatch):
    from paper_agent.agent.wiki_research import collect_wiki_evidence

    wiki = tmp_path / 'wiki'
    page = wiki / 'papers/example.md'
    page.parent.mkdir(parents=True)
    page.write_text('# Example\n## Method\nRelaxation predicts capacity.\n')
    monkeypatch.setenv('PAPER_AGENT_WIKI_ROOT', str(wiki))
    monkeypatch.delenv('PAPER_AGENT_EMBEDDING_DIR', raising=False)
    monkeypatch.delenv('PAPER_AGENT_EMBEDDING_PYTHON', raising=False)
    before = {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob('*') if p.is_file()}

    class Reader:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            assert {tool['function']['name'] for tool in tools} <= {'search_wiki'}
            if self.calls == 1:
                return ModelResponse(tool_calls=(ToolCall(
                    'search', 'search_wiki', {'query': 'Relaxation'},
                ),))
            return ModelResponse(content=json.dumps({
                'summary': 'Capacity prediction evidence', 'assessment': 'sufficient',
                'evidence': [{'claim': 'Relaxation is used to predict capacity.',
                              'role': 'direct_method', 'wiki_path': 'papers/example.md',
                              'section': 'Method', 'quote': 'Relaxation predicts capacity.'}],
                'decisions': [], 'hypotheses': [], 'gaps': [], 'next_action': 'Review evidence',
            }))

    packet = collect_wiki_evidence(tmp_path / 'project', Reader(), 'Relaxation')
    assert packet['status'] == 'ready_for_review'
    assert packet['evidence'][0]['content_sha256']
    assert not packet['validation_errors']
    assert before == {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob('*') if p.is_file()}
