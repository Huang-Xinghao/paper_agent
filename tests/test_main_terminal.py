import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent import coordinator, herdr, main_terminal
from paper_agent.agent.workflow_cli import _write_json
from paper_agent.agent.workflow import WorkflowState
from paper_agent.agent.model_client import ModelResponse, ToolCall


def test_start_opens_main_only_without_prestarting_roles(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    calls = []
    def call(self, *args):
        calls.append(args)
        if args[:2] == ('pane', 'current'):
            return {'pane': {'pane_id': 'w1:p1'}}
        return {}
    monkeypatch.setattr(herdr.Client, 'call', call)
    opened = []
    monkeypatch.setattr(main_terminal.MainTerminal, 'run', lambda self: opened.append(self.role))
    monkeypatch.setattr(sys, 'argv', ['paper-main', '--project', str(tmp_path), '--session', 'test-main'])
    main_terminal.main()
    assert opened == ['main']
    assert ('pane', 'zoom', 'w1:p1', '--on') in calls
    assert not (herdr.directory(tmp_path) / 'roles').exists()
    assert not any(c[:2] in {('pane', 'split'), ('pane', 'run')} for c in calls)


def turn(tmp_path):
    folder = tmp_path / '.paper-agent/main-terminal/turns/test-turn'
    _write_json(folder / 'request.json', {'project': str(tmp_path), 'prompt': 'Discuss first', 'options': {}})
    return folder


def test_turn_calls_real_coordinator_interface_and_does_not_replay(tmp_path, monkeypatch):
    folder = turn(tmp_path)
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    def go(self, prompt):
        assert prompt == 'Discuss first'
        self.on_event({'kind': 'assignment_start', 'assignment': {'node': 'literature',
            'role': 'idea_generator', 'instruction': 'Review evidence'}})
        return SimpleNamespace(status='awaiting_user', error=None, final='需要你选择研究方向。')
    monkeypatch.setattr(coordinator.CoordinatorAgent, 'go', go)
    main_terminal.run_turn(folder)
    assert json.loads((folder / 'result.json').read_text())['status'] == 'awaiting_user'
    assert json.loads((folder / 'events.jsonl').read_text())['kind'] == 'assignment_start'
    with pytest.raises(RuntimeError, match='already started'):
        main_terminal.run_turn(folder)


def test_cancel_waits_for_existing_role_receipt_before_returning(tmp_path, monkeypatch):
    folder = turn(tmp_path)
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    _write_json(herdr.directory(tmp_path) / 'active.json', {'id': 'existing', 'consumed': False})
    def stop(self, prompt):
        self.on_event({'kind': 'herdr_submitted', 'herdr_task_id': 'existing'})
        raise KeyboardInterrupt
    monkeypatch.setattr(coordinator.CoordinatorAgent, 'go', stop)
    checked = []
    monkeypatch.setattr(herdr, 'reconcile', lambda project, task, **kw: checked.append(task))
    main_terminal.run_turn(folder)
    assert checked == ['existing']
    assert json.loads((folder / 'result.json').read_text())['status'] == 'interrupted'


def test_main_ui_displays_public_stream_and_actions_without_duplicate_final(tmp_path):
    import asyncio
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    root = tmp_path / '.paper-agent/main-terminal'
    root.mkdir(parents=True)
    with create_pipe_input(), create_app_session(output=DummyOutput()):
        shown = []
        ui = main_terminal.MainTerminal(tmp_path, root, lambda s: None, show_roles=shown.append)
        ui.event({'kind': 'tool_start', 'phase': 'coordinator', 'tool': 'read_project_file',
                  'arguments': json.dumps({'path': 'literature/RESEARCH_REPORT.md'})})
        assert '读取文件' in ui.transcript.text
        assert 'literature/RESEARCH_REPORT.md' in ui.transcript.text
        ui.event({'kind': 'herdr_submitted', 'node': 'literature'})
        assert shown == [False]  # Keep the main readable in a narrow terminal.
        for kind, content in [('assistant_stream_start', ''), ('assistant_delta', '公开回复'),
                              ('assistant_delta', '逐步到达')]:
            ui.event({'kind': kind, 'phase': 'coordinator', 'iteration': 1, 'content': content})
        assert '公开回复逐步到达' in ui.transcript.text
        ui.event({'kind': 'assistant_output', 'phase': 'coordinator', 'iteration': 1,
                  'content': '公开回复逐步到达'})
        assert ui.transcript.text.count('公开回复逐步到达') == 1
        assert ui.last_output == '公开回复逐步到达'
        for kind, content in [('model_start', ''), ('assistant_stream_start', ''),
                              ('assistant_delta', '已核实数据划分，接下来编写实验计划。'),
                              ('assistant_output', '已核实数据划分，接下来编写实验计划。')]:
            ui.event({'kind': kind, 'phase': 'experiment_plan', 'role': 'experiment',
                      'iteration': 2, 'content': content})
        assert '● Experiment' in ui.transcript.text
        assert ui.transcript.text.count('已核实数据划分') == 1
        assert 'Experiment' in ui.activity_label


def test_main_expands_on_wide_terminals_and_collapses_between_stages(tmp_path):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.data_structures import Size
    class WideOutput(DummyOutput):
        def get_size(self):
            return Size(rows=40, columns=160)
    root = tmp_path / '.paper-agent/main-terminal'
    root.mkdir(parents=True)
    with create_pipe_input(), create_app_session(output=WideOutput()):
        shown = []
        ui = main_terminal.MainTerminal(tmp_path, root, lambda s: None, show_roles=shown.append)
        ui.event({'kind': 'herdr_submitted', 'node': 'experiment_plan'})
        assert shown == [True]
        ui.event({'kind': 'assignment_end', 'node': 'experiment_plan', 'status': 'completed'})
        assert shown == [True, False]
        assert ui.activity_label == '主控正在核对结果'
        assert 'completed' in ui.transcript.text
        ui.event({'kind': 'herdr_submitted', 'node': 'experiment_bridge'})
        assert shown == [True, False, True]
        ui.event({'kind': 'assignment_end', 'node': 'experiment_bridge', 'status': 'awaiting_user'})
        assert shown == [True, False, True, False]


def pending_proposal(project, *, stop_after=None):
    report = project / 'refine-logs/FINAL_PROPOSAL.md'
    report.parent.mkdir()
    report.write_text('# Approved research design\nFive Ridge groups and training-side diagnostics.')
    state = WorkflowState('approval-test', project_goal='Original research goal', mode='expert',
        current_node='refine', status='awaiting_user', workflow_stop_after=stop_after,
        expert_checkpoint={'node': 'refine', 'status': 'pending',
            'report': 'refine-logs/FINAL_PROPOSAL.md', 'next_node': 'experiment_plan',
            'stop_after': 'refine'})
    _write_json(project / 'workflow-state.json', state.to_dict())
    return report


@pytest.mark.parametrize('prompt, snapshot_request', [
    ('批准', False), ('批准推荐方案，继续实验规划', True),
    ('可以的 开始', False), ('同意当前方案并继续', True)])
def test_approval_turn_validates_persists_and_delegates_planning(tmp_path, monkeypatch, prompt, snapshot_request):
    import hashlib
    report = pending_proposal(tmp_path)
    folder = turn(tmp_path)
    # This is also the request format sent by an already-running old UI host.
    request = {'project': str(tmp_path), 'prompt': prompt, 'options': {}}
    if snapshot_request:
        request['checkpoint'] = main_terminal.checkpoint_snapshot(tmp_path)
    _write_json(folder / 'request.json', request)
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    original = coordinator.CoordinatorAgent
    calls = []

    def factory(*args, **kwargs):
        assert kwargs['first_node'] == kwargs['stop_after'] == 'experiment_plan'
        task = kwargs['stage_task']
        class Model:
            called = False
            def complete(self, messages, tools):
                if not self.called:
                    self.called = True
                    assert '检查点未解除”的描述已经过时' in str(messages)
                    return ModelResponse(tool_calls=(ToolCall('plan', 'delegate_stage', {'assignment': task}),))
                return ModelResponse(content='已完成实验规划，尚未训练。')
        return original(*args, model_client=Model(), **kwargs)

    def execute(project, node, instruction, **options):
        calls.append(node)
        saved = WorkflowState.from_dict(json.loads((project / 'workflow-state.json').read_text()))
        assert saved.status == 'running' and saved.current_node == 'experiment_plan'
        assert saved.expert_checkpoint['status'] == 'approved'
        assert saved.history[-1]['event'] == 'expert_approved'
        assert saved.history[-1]['sha256'] == hashlib.sha256(report.read_bytes()).hexdigest()
        assert saved.project_goal == options['project_goal'] == 'Original research goal'
        saved.current_node, saved.status = node, 'completed'
        saved.expert_checkpoint = None
        return saved

    monkeypatch.setattr(coordinator, 'CoordinatorAgent', factory)
    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    main_terminal.run_turn(folder)
    assert calls == ['experiment_plan']
    assert json.loads((folder / 'result.json').read_text())['status'] == 'completed'
    events = [json.loads(line) for line in (folder / 'events.jsonl').read_text().splitlines()]
    assert events[0]['kind'] == 'checkpoint_approved'


@pytest.mark.parametrize('prompt', ['不批准', '批准了吗？', '可以批准吗', '批准，但先改成两组',
                                  '先别开始', '解释一下方案', '继续', '可以的 开始？'])
def test_discussion_does_not_approve_and_reports_pending_status(tmp_path, monkeypatch, prompt):
    pending_proposal(tmp_path)
    before = (tmp_path / 'workflow-state.json').read_bytes()
    folder = turn(tmp_path)
    _write_json(folder / 'request.json', {'project': str(tmp_path), 'prompt': prompt, 'options': {}})
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    original = coordinator.CoordinatorAgent
    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content='当前方案等待审批。')
    monkeypatch.setattr(coordinator, 'CoordinatorAgent',
        lambda *a, **kw: original(*a, model_client=Model(), **kw))
    main_terminal.run_turn(folder)
    assert (tmp_path / 'workflow-state.json').read_bytes() == before
    assert json.loads((folder / 'result.json').read_text())['status'] == 'awaiting_user'


@pytest.mark.parametrize('change', ['report', 'state', 'empty', 'pending_task'])
def test_approval_rejects_changed_or_unfinished_work(tmp_path, monkeypatch, change):
    report = pending_proposal(tmp_path)
    snapshot = main_terminal.checkpoint_snapshot(tmp_path)
    if change in {'report', 'empty'}:
        report.write_text('New design' if change == 'report' else '')
        if change == 'empty':
            snapshot = main_terminal.checkpoint_snapshot(tmp_path)
    elif change == 'state':
        state = json.loads((tmp_path / 'workflow-state.json').read_text())
        state['expert_checkpoint']['summary'] = 'New checkpoint'
        _write_json(tmp_path / 'workflow-state.json', state)
    else:
        _write_json(herdr.directory(tmp_path) / 'active.json', {'id': 'pending', 'consumed': False})
    before = (tmp_path / 'workflow-state.json').read_bytes()
    folder = turn(tmp_path)
    _write_json(folder / 'request.json', {'project': str(tmp_path), 'prompt': '批准',
                                        'options': {}, 'checkpoint': snapshot})
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    monkeypatch.setattr(coordinator, 'CoordinatorAgent', lambda *a, **kw: pytest.fail('Must not run main'))
    main_terminal.run_turn(folder)
    assert json.loads((folder / 'result.json').read_text())['status'] == 'failed'
    assert (tmp_path / 'workflow-state.json').read_bytes() == before


def test_approval_respects_user_stop_boundary_without_launching_main(tmp_path, monkeypatch):
    pending_proposal(tmp_path, stop_after='refine')
    folder = turn(tmp_path)
    _write_json(folder / 'request.json', {'project': str(tmp_path), 'prompt': '批准', 'options': {}})
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    monkeypatch.setattr(coordinator, 'CoordinatorAgent', lambda *a, **kw: pytest.fail('Stop boundary reached'))
    main_terminal.run_turn(folder)
    state = json.loads((tmp_path / 'workflow-state.json').read_text())
    assert state['status'] == 'completed'
    assert state['expert_checkpoint']['status'] == 'approved'


def test_approval_cannot_bypass_unreviewed_proposal_draft(tmp_path, monkeypatch):
    import paper_agent.proposal_review as review
    pending_proposal(tmp_path)
    before = (tmp_path / 'workflow-state.json').read_bytes()
    def reject(project):
        raise ValueError('存在方案审阅草稿')
    monkeypatch.setattr(review, 'guard_legacy_approval', reject)
    with pytest.raises(ValueError, match='方案审阅草稿'):
        main_terminal.approve_checkpoint(tmp_path, main_terminal.checkpoint_snapshot(tmp_path))
    assert (tmp_path / 'workflow-state.json').read_bytes() == before
