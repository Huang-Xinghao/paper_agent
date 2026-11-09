import json
from types import SimpleNamespace

import pytest

from paper_agent.agent import coordinator, workflow_cli, node_task, herdr_role, model_client
from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.role_sessions import RoleSession, PhaseTask
from paper_agent.agent.workflow import WorkflowState, WorkflowRunner, NodeResult
from paper_agent.agent.workflow_cli import _write_json
from paper_agent.agent.main_terminal import checkpoint_snapshot, approve_checkpoint


class Model:
    def __init__(self, *steps, **kwargs):
        self.steps = iter(steps)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.steps)


def call(name, **args):
    return ModelResponse(tool_calls=(ToolCall(name, name, args),))


def pending(project):
    path = project / 'refine-logs/FINAL_PROPOSAL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Old proposal')
    (project / 'idea-stage').mkdir()
    (project / 'idea-stage/IDEA_REPORT.md').write_text('Original idea')
    state = WorkflowState('test', project_goal='Research', mode='expert', current_node='refine',
                          status='awaiting_user', expert_checkpoint={
                              'node': 'refine', 'status': 'pending', 'report': 'refine-logs/FINAL_PROPOSAL.md',
                              'files': ['refine-logs/FINAL_PROPOSAL.md'], 'next_node': 'experiment_plan'})
    _write_json(project / 'workflow-state.json', state.to_dict())
    return state


@pytest.mark.parametrize('author', ['idea_generator', 'paper_writer'])
def test_checkpoint_feedback_reaches_original_role_new_version_requires_approval(tmp_path, monkeypatch, author):
    state = pending(tmp_path)
    state.node_results['refine'] = {'role': author}
    _write_json(tmp_path / 'workflow-state.json', state.to_dict())
    PhaseAgent(tmp_path, model_client=Model(ModelResponse(content='Original author memory'))).execute(
        PhaseTask('novelty', 'Remember the original rationale', role=author), completion_check=lambda: None)
    old_snapshot = checkpoint_snapshot(tmp_path)
    worker = Model(call('write', path='refine-logs/FINAL_PROPOSAL.md', content='Revised proposal: method B'),
                   call('write', path='refine-logs/refine_EXPERT_CHECKPOINT.json', content=json.dumps({
                       'summary': 'Method B', 'questions': ['接受 B 吗？', '范围是否合适？'],
                       'brief': {key: 'Revised proposal: method B' for key in
                                 ('研究问题', '方法与创新', '关键假设与证据', '成败判据')},
                       'decision_options': [['采用 B', '保留 A'], ['扩大', '缩小']]})),
                   ModelResponse(content='新方案已交付'))
    def make_agent(project, **kwargs):
        kwargs.pop('model', None)
        return PhaseAgent(project, model_client=worker, **kwargs)
    monkeypatch.setattr(workflow_cli, 'PhaseAgent', make_agent)
    # Keep real expert routing/validators; replace unrelated research retrieval.
    monkeypatch.setattr(WorkflowRunner, '_run_refine', lambda self, state, prompt:
        (self._go(prompt, phase='research_refine', completion_check=lambda: None) and NodeResult('proposal')))
    monkeypatch.setattr(coordinator, 'execute_stage', workflow_cli.execute_stage)
    model = Model(call('revise_checkpoint', node='refine', instruction='将方法改成 B，保留原目标'),
                  ModelResponse(content='已按意见生成 B 方案，请审阅新版。'))
    result = CoordinatorAgent(tmp_path, model_client=model).go('将方法改成 B，保留原目标')
    assert result.status == 'awaiting_user', result.error
    assert '将方法改成 B' in json.dumps(worker.requests, ensure_ascii=False)
    role = RoleSession(tmp_path, author).load()
    assert role['tasks'][-1]['phase'] == 'research_refine'
    assert 'Original author memory' in json.dumps(worker.requests[0])
    assert 'Revised proposal' in (tmp_path / 'refine-logs/FINAL_PROPOSAL.md').read_text()
    state = json.loads((tmp_path / 'workflow-state.json').read_text())
    assert state['expert_checkpoint']['status'] == 'pending'
    assert state['current_node'] == 'refine'
    with pytest.raises(ValueError, match='已变化'):
        approve_checkpoint(tmp_path, old_snapshot)
    approved, _ = approve_checkpoint(tmp_path, checkpoint_snapshot(tmp_path))
    assert approved.expert_checkpoint['status'] == 'approved'
    assert approved.current_node == 'experiment_plan'


@pytest.mark.parametrize('later_checkpoint', [False, True])
@pytest.mark.parametrize('author', ['idea_generator', 'paper_writer'])
def test_scoped_revision_of_approved_proposal_reopens_approval(tmp_path, monkeypatch, later_checkpoint, author):
    state = pending(tmp_path)
    state.status = 'completed'
    state.expert_checkpoint['status'] = 'approved'
    state.node_results['refine'] = {'role': author}
    if later_checkpoint:
        (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('Plan for old proposal')
        state.status, state.current_node = 'awaiting_user', 'experiment_plan'
        state.expert_checkpoint.update(node='experiment_plan', status='pending',
                                       report='refine-logs/EXPERIMENT_PLAN.md',
                                       files=['refine-logs/EXPERIMENT_PLAN.md'])
    _write_json(tmp_path / 'workflow-state.json', state.to_dict())
    worker = Model(call('write', path='refine-logs/FINAL_PROPOSAL.md', content='Scoped method B'),
                   ModelResponse(content='已修订'))
    def make_agent(project, **kwargs):
        kwargs.pop('model', None)
        return PhaseAgent(project, model_client=worker, **kwargs)
    monkeypatch.setattr(node_task, 'PhaseAgent', make_agent)
    task = {'node': 'refine', 'target': 'proposal', 'instruction': '只改为 B',
            'files': ['refine-logs/FINAL_PROPOSAL.md']}
    result = CoordinatorAgent(tmp_path, model_client=Model(call('revise_artifact', task=task),
                              ModelResponse(content='请审批新版'))).go('只改为 B')
    assert result.status == 'awaiting_user', result.error
    saved = json.loads((tmp_path / 'workflow-state.json').read_text())
    assert saved['expert_checkpoint']['status'] == 'pending'
    assert saved['expert_checkpoint']['node'] == 'refine'
    assert 'confirmed_sha256' not in saved['expert_checkpoint']
    assert RoleSession(tmp_path, author).load()['tasks'][-1]['phase'] == 'research_refine'
    if later_checkpoint:
        from paper_agent.agent.artifact_versions import ArtifactVersions
        assert 'experiment_plan' in ArtifactVersions(tmp_path).context()['stale_nodes']


def test_direct_role_discussion_does_not_change_later_execution_memory(tmp_path, monkeypatch):
    from paper_agent.agent.herdr import directory
    directory(tmp_path).mkdir(parents=True)
    first = PhaseAgent(tmp_path, model_client=Model(ModelResponse(content='正式采用 A')))
    first.execute(PhaseTask('ideas', '正式任务采用 A'), completion_check=lambda: None)
    session = RoleSession(tmp_path, 'idea_generator')
    original = session.path.read_bytes()
    discussion = Model(ModelResponse(content='仅讨论 B，不会落实'))
    monkeypatch.setattr(model_client, 'ModelClient', lambda **kwargs: discussion)
    herdr_role.chat(tmp_path, 'idea_generator', '讨论一下改成 B')
    assert session.path.read_bytes() == original
    model = Model(ModelResponse(content='继续 A'))
    PhaseAgent(tmp_path, model_client=model).execute(PhaseTask('research_refine', '继续正式任务'),
                                                     completion_check=lambda: None)
    history = json.dumps(model.requests[0], ensure_ascii=False)
    assert '正式采用 A' in history
    assert '讨论一下改成 B' not in history and '仅讨论 B' not in history


@pytest.mark.parametrize('edited', ['paper/references.bib', 'paper/sections/intro.tex'])
def test_approval_snapshot_includes_secondary_files(tmp_path, edited):
    state = pending(tmp_path)
    state.current_node = 'paper_write'
    (tmp_path / 'paper').mkdir()
    (tmp_path / 'paper/main.tex').write_text('main')
    (tmp_path / 'paper/references.bib').write_text('refs v1')
    (tmp_path / 'paper/sections').mkdir()
    (tmp_path / 'paper/sections/intro.tex').write_text('intro v1')
    state.expert_checkpoint.update(node='paper_write', report='paper/main.tex',
                                  files=['paper/main.tex', 'paper/references.bib'])
    _write_json(tmp_path / 'workflow-state.json', state.to_dict())
    old = checkpoint_snapshot(tmp_path)
    (tmp_path / edited).write_text('v2')
    assert old != checkpoint_snapshot(tmp_path)
    with pytest.raises(ValueError, match='已变化'):
        approve_checkpoint(tmp_path, old)


def test_legacy_mixed_memory_is_archived_and_discussion_removed_before_execution(tmp_path):
    session = RoleSession(tmp_path, 'idea_generator')
    with session.locked():
        data = session.load()
        data.pop('channel')
        data['messages'] = [
            {'role': 'user', 'content': 'Current assignment [old], phase=ideas: execute A'},
            {'role': 'assistant', 'content': 'Verified method A'},
            {'role': 'user', 'content': 'casual suggestion: change everything to B'},
            {'role': 'assistant', 'content': 'Uncommitted suggestion B'},
        ]
        data['context_memory'] = {'summary': 'Potentially mixed B'}
        session.save(data)
    model = Model(ModelResponse(content='Continue A'))
    PhaseAgent(tmp_path, model_client=model).execute(PhaseTask('research_refine', 'Continue formal A'),
                                                    completion_check=lambda: None)
    history = json.dumps(model.requests[0])
    assert 'Verified method A' in history
    assert 'casual suggestion' not in history and 'Potentially mixed B' not in history
    archive = list((session.folder / 'archives/idea_generator/legacy-mixed').glob('*.json'))
    assert len(archive) == 1 and 'Uncommitted suggestion B' in archive[0].read_text()


def test_real_delegated_role_question_routes_through_main_and_resumes_once(tmp_path, monkeypatch):
    from paper_agent.agent.user_questions import answer_command, UserQuestions
    from paper_agent.agent.artifact_versions import ArtifactVersions
    worker = Model(call('write', path='progress.md', content='completed before question'),
                   call('request_user_decision', question='采用哪个方向？', options=['A', 'B']),
                   call('write', path='literature/RESEARCH_REPORT.md', content='Evidence for direction B'),
                   ModelResponse(content='报告完成'))
    def make_agent(project, **kwargs):
        kwargs.pop('model', None)
        return PhaseAgent(project, model_client=worker, **kwargs)
    monkeypatch.setattr(workflow_cli, 'PhaseAgent', make_agent)
    monkeypatch.setattr(WorkflowRunner, '_research_context', lambda *a, **k: {})
    monkeypatch.setattr(coordinator, 'execute_stage', workflow_cli.execute_stage)
    events = []
    def event(record):
        events.append(record)
        if record['kind'] == 'user_question':
            assert record['role'] == 'idea_generator'
            assert (tmp_path / 'progress.md').exists()
            answer_command(tmp_path, '/answer B')
    main_model = Model(call('delegate_stage', assignment={
        'node': 'literature', 'role': 'idea_generator', 'instruction': '确定方向后交付文献报告'}),
        ModelResponse(content='已核对方向 B 的报告'))
    result = CoordinatorAgent(tmp_path, model_client=main_model, on_event=event).go('研究文献')
    assert result.status == 'completed', result.error
    data = RoleSession(tmp_path, 'idea_generator').load()
    assert len(data['tasks']) == 1
    assert [m.get('name') for m in data['messages'] if m['role'] == 'tool'] == [
        'write', 'request_user_decision', 'write']
    assert json.loads(worker.requests[2][-1]['content'])['answer'] == 'B'
    assert UserQuestions(tmp_path).pending() == []
