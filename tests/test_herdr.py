"""Transport-contract tests; no running Herdr server or paid model required."""
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from paper_agent.agent import herdr, herdr_worker, stage_backend, workflow_cli
from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.role_sessions import RoleSession
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w7:p1')
    calls = []
    jobs = []
    def call(self, *args):
        calls.append(args)
        if args[:2] == ('pane', 'current'):
            return {'pane': {'pane_id': 'w7:p1'}}
        if args[:2] == ('pane', 'get'):
            assert args[2] == 'w7:p1'
            return {'pane': {'pane_id': 'w7:p1'}}
        if args[:2] == ('pane', 'split'):
            assert '--no-focus' in args
            assert args[args.index('--cwd') + 1] == str(tmp_path)
            return {'pane': {'pane_id': 'w7:p9'}}
        if args[:2] == ('pane', 'report-agent'):
            assert args[2] == 'w7:p9'
            return {}
        pytest.fail(f'Unexpected Herdr command: {args}')
    monkeypatch.setattr(herdr.Client, 'call', call)
    herdr.connect(tmp_path, 'paper-test', '/test/herdr')
    monkeypatch.setattr(herdr, 'ensure_role', lambda *a: {'pane_id': 'w7:p9'})
    original_write = herdr._write_json
    def write(path, value):
        original_write(path, value)
        if path.name == 'inbox.json':
            folder = herdr.directory(tmp_path) / 'tasks' / value['id']
            jobs.append(folder)
            with monkeypatch.context() as child:
                child.setenv('HERDR_PANE_ID', 'w7:p9')
                herdr_worker.run(folder)
    monkeypatch.setattr(herdr, '_write_json', write)
    # Exercise the real role-session runtime, file tools, single-node runner and receipt.
    class Model:
        def complete(self, messages, tools):
            if not any(m['role'] == 'tool' for m in messages):
                return ModelResponse(tool_calls=(ToolCall('write', 'write', {
                    'path': 'literature/RESEARCH_REPORT.md', 'content': 'Verified literature evidence; unresolved gap.'}),))
            return ModelResponse(content='Evidence written.')
    original = PhaseAgent
    monkeypatch.setattr(workflow_cli, 'PhaseAgent', lambda *a, **kw: original(*a, model_client=Model(), **kw))
    monkeypatch.setattr(WorkflowRunner, '_research_context', lambda *a, **kw: {})
    return calls, jobs


def test_real_coordinator_worker_artifact_receipt_chain(tmp_path, monkeypatch, transport):
    calls, jobs = transport
    events = []
    class MainModel:
        turn = 0
        def complete(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return ModelResponse(tool_calls=(ToolCall('delegate', 'delegate_stage', {'assignment': {
                    'node': 'literature', 'role': 'idea_generator', 'instruction': 'Find the evidence gap'}}),))
            receipt = json.loads(messages[-1]['content'])
            assert receipt['status'] == 'completed'
            assert receipt['artifact_handoff'][0]['sha256']
            assert receipt['result']['execution']['backend'] == 'herdr'
            assert receipt['result']['execution']['herdr_task_id']
            return ModelResponse(content='已检查文献产物。')
    result = CoordinatorAgent(tmp_path, model_client=MainModel(), stop_after='literature', on_event=events.append).go('Study the research question')
    assert result.status == 'completed'
    assert len(jobs) == 1
    request = json.loads((jobs[0] / 'request.json').read_text())
    assert request['instruction'] == 'Find the evidence gap'
    assert request['project_goal'] == 'Study the research question'
    receipt = json.loads((jobs[0] / 'result.json').read_text())
    assert receipt['state_sha256']
    state = json.loads((tmp_path / 'workflow-state.json').read_text())
    assert state['node_results']['literature']['execution']['herdr_task_id'] == receipt['id']
    assert state['artifacts']['literature'][0]['sha256']
    assert json.loads((herdr.directory(tmp_path) / 'active.json').read_text())['consumed']
    session = RoleSession(tmp_path, 'idea_generator').load()
    assert session['tasks'][-1]['status'] == 'completed'
    assert any(e['kind'] == 'role_task_end' for e in events)
    assert any(e['kind'] == 'herdr_submitted' for e in events)
    assert any(c[:2] == ('pane', 'report-agent') for c in calls)


def test_options_round_trip_and_wait_reconcile(tmp_path, transport):
    _, jobs = transport
    state = herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal',
                         config=WorkflowConfig(max_experiment_repairs=0), max_iterations=3)
    receipt = json.loads((jobs[0] / 'result.json').read_text())
    assert herdr.wait(tmp_path, receipt['id']).status == state.status == 'completed'
    assert json.loads((jobs[0] / 'request.json').read_text())['options']['config']['max_experiment_repairs'] == 0
    (tmp_path / 'workflow-state.json').write_text('{}')
    with pytest.raises(herdr.HerdrError, match='changed'):
        herdr.wait(tmp_path, receipt['id'])


def test_lost_delivery_never_resubmits_existing_job(tmp_path, transport, monkeypatch):
    calls, jobs = transport
    original = herdr._write_json
    def lost(path, value):
        original(path, value)
        if path.name == 'inbox.json':
            raise herdr.HerdrError('response lost after actual execution')
    monkeypatch.setattr(herdr, '_write_json', lost)
    options = dict(role='idea_generator', project_goal='Goal')
    with pytest.raises(herdr.HerdrError, match='response lost'):
        herdr.submit(tmp_path, 'literature', 'Find gap', **options)
    with pytest.raises(herdr.HerdrError, match='Previous task'):
        herdr.submit(tmp_path, 'literature', 'Find gap', **options)
    assert len(jobs) == 1
    task_id = jobs[0].name
    assert herdr.wait(tmp_path, task_id).status == 'completed'
    with pytest.raises(herdr.HerdrError, match='already started'):
        herdr_worker.run(jobs[0])


def test_outside_herdr_fails_before_mutations(tmp_path, monkeypatch):
    monkeypatch.delenv('HERDR_ENV', raising=False)
    with pytest.raises(herdr.HerdrError, match='inside'):
        herdr.connect(tmp_path, 'paper-test')
    assert not herdr.directory(tmp_path).exists()


def test_web_launch_default_binds_new_project_without_overriding_explicit_binding(tmp_path, transport, monkeypatch):
    config_path = herdr.directory(tmp_path) / 'config.json'
    config_path.unlink()
    monkeypatch.setenv('PAPER_AGENT_HERDR_SESSION', 'paper-test')
    state = stage_backend.dispatch_stage(tmp_path, 'literature', 'Find gap',
                                        role='idea_generator', project_goal='Goal')
    assert state.status == 'completed'
    assert herdr.load_config(tmp_path)['session'] == 'paper-test'
    # The launch default must never silently move an already-bound project.
    monkeypatch.setenv('PAPER_AGENT_HERDR_SESSION', 'different-session')
    stage_backend.dispatch_stage(tmp_path, 'literature', 'Find another gap',
                                 role='idea_generator', project_goal='Goal')
    assert herdr.load_config(tmp_path)['session'] == 'paper-test'


def test_web_launch_default_outside_herdr_rejects_instead_of_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('PAPER_AGENT_HERDR_SESSION', 'paper-test')
    monkeypatch.delenv('HERDR_ENV', raising=False)
    with pytest.raises(herdr.HerdrError, match='inside'):
        stage_backend.dispatch_stage(tmp_path, 'literature', 'Find gap')
    assert not herdr.directory(tmp_path).exists()


def test_unconfigured_nodes_keep_local_executor(tmp_path, monkeypatch, transport):
    called = []
    monkeypatch.setattr(stage_backend, 'execute_stage', lambda *a, **kw: called.append(a))
    (herdr.directory(tmp_path) / 'config.json').unlink()
    stage_backend.dispatch_stage(tmp_path, 'literature', 'Only literature', role='idea_generator', project_goal='Goal')
    assert len(called) == 1


def test_worker_failure_returns_failed_receipt(tmp_path, transport, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('model call failed')
    monkeypatch.setattr(herdr_worker, 'execute_stage', fail)
    state = herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal')
    assert state.status == 'failed'
    assert state.error == 'model call failed'
    assert state.failed_node == 'literature'


def test_timeout_keeps_original_job_pending(tmp_path, transport, monkeypatch):
    monkeypatch.setattr(herdr, '_write_json', workflow_cli._write_json)
    config = herdr.load_config(tmp_path)
    config['wait_seconds'] = 0.001
    workflow_cli._write_json(herdr.directory(tmp_path) / 'config.json', config)
    monkeypatch.setattr(herdr.time, 'sleep', lambda seconds: None)
    with pytest.raises(herdr.HerdrError, match='outcome pending'):
        herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal')
    active = json.loads((herdr.directory(tmp_path) / 'active.json').read_text())
    assert not active['consumed']
    assert active['delivery'] == 'submitted'
    assert not (herdr.directory(tmp_path) / 'tasks' / active['id'] / 'cancel').exists()


def test_waiting_for_user_does_not_consume_worker_timeout(tmp_path, monkeypatch):
    import hashlib
    import os
    from paper_agent.agent.user_questions import UserQuestions
    from paper_agent.agent.workflow import WorkflowState
    task_id = 'a' * 32
    folder = herdr.directory(tmp_path) / 'tasks' / task_id
    workflow_cli._write_json(folder / 'request.json', {'project': str(tmp_path), 'node': 'literature',
                                                     'project_goal': 'Research'})
    workflow_cli._write_json(folder / 'started.json', {'pid': os.getpid()})
    UserQuestions(tmp_path).create('请选择', role='idea_generator', task_id='role-task', phase='literature')
    tick = iter([0, .5])
    monkeypatch.setattr(herdr.time, 'monotonic', lambda: next(tick))
    monkeypatch.setattr(herdr.time, 'sleep', lambda seconds: None)
    polls = []
    def relay(*args):
        polls.append(1)
        if len(polls) == 2:
            state = WorkflowState('test', project_goal='Research', current_node='literature', status='completed',
                                  node_results={'literature': {'execution': {'herdr_task_id': task_id}}})
            workflow_cli._write_json(tmp_path / 'workflow-state.json', state.to_dict())
            workflow_cli._write_json(folder / 'result.json', {'id': task_id, 'status': 'completed',
                'state_sha256': hashlib.sha256((tmp_path / 'workflow-state.json').read_bytes()).hexdigest()})
        return 0
    monkeypatch.setattr(herdr, '_relay', relay)
    result = herdr.wait(tmp_path, task_id, wait_seconds=.1)
    assert result.status == 'completed'


def test_interrupt_requests_cooperative_cancellation(tmp_path, transport, monkeypatch):
    monkeypatch.setattr(herdr, '_write_json', workflow_cli._write_json)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(herdr, 'wait', interrupt)
    with pytest.raises(KeyboardInterrupt):
        herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal')
    active = json.loads((herdr.directory(tmp_path) / 'active.json').read_text())
    folder = herdr.directory(tmp_path) / 'tasks' / active['id']
    assert (folder / 'cancel').is_file()
    # A late worker sees cancellation and records interruption without executing a stage.
    monkeypatch.setenv('HERDR_PANE_ID', 'w7:p9')
    herdr_worker.run(folder)
    assert json.loads((folder / 'result.json').read_text())['status'] == 'interrupted'
    assert not (tmp_path / 'literature/RESEARCH_REPORT.md').exists()


def test_malformed_config_and_wrong_session_do_not_fallback(tmp_path, transport, monkeypatch):
    config_path = herdr.directory(tmp_path) / 'config.json'
    config = json.loads(config_path.read_text())
    config['nodes'] = ['experiment_bridge']
    workflow_cli._write_json(config_path, config)
    with pytest.raises(ValueError, match='reconnect'):
        stage_backend.dispatch_stage(tmp_path, 'literature', 'Find gap')
    monkeypatch.setattr(herdr.Client, 'call', lambda *a: {'pane': {'pane_id': 'other:p1'}})
    with pytest.raises(herdr.HerdrError, match='does not match'):
        herdr.connect(tmp_path, 'wrong-session', '/test/herdr')


def test_json_client_has_no_mutation_retry_and_quotes_command(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w7:p1')
    calls = []
    def execute(argv, **options):
        calls.append((argv, options))
        assert options['timeout'] == 30
        assert not options.get('shell')
        return SimpleNamespace(returncode=0, stdout='{"result":{"pane":{"pane_id":"w7:p1"}}}', stderr='')
    monkeypatch.setattr(herdr.subprocess, 'run', execute)
    client = herdr.Client('paper-test', '/path with spaces/herdr')
    assert client.call('pane', 'current', '--current')['pane']['pane_id'] == 'w7:p1'
    assert calls[0][0][0] == '/path with spaces/herdr'
    assert calls[0][0][1:3] == ['--session', 'paper-test']
    monkeypatch.setattr(herdr.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert client.call('pane', 'run', 'w7:p1', 'true') == {'queued': True}
    assert client.call('pane', 'report-agent', 'w7:p1', '--state', 'idle') == {}
    # Empty reads still indicate an invalid response, not successful discovery.
    with pytest.raises(herdr.HerdrError, match='response unknown'):
        client.call('pane', 'current', '--current')
    def error(*a, **k):
        calls.append('failed')
        return SimpleNamespace(returncode=1, stdout='', stderr='{"error":{"message":"delivery unknown"}}')
    monkeypatch.setattr(herdr.subprocess, 'run', error)
    with pytest.raises(herdr.HerdrError, match='delivery unknown'):
        client.call('pane', 'run', 'w7:p1', 'a command')
    assert calls.count('failed') == 1


def test_changed_artifact_rejects_stale_success(tmp_path, transport):
    _, jobs = transport
    herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal')
    (tmp_path / 'literature/RESEARCH_REPORT.md').write_text('Changed after execution')
    with pytest.raises(herdr.HerdrError, match='Artifact changed'):
        herdr.wait(tmp_path, jobs[0].name)


def test_wait_cli_consumes_existing_result_without_new_command(tmp_path, transport, monkeypatch, capsys):
    _, jobs = transport
    herdr.submit(tmp_path, 'literature', 'Find gap', role='idea_generator', project_goal='Goal')
    active_path = herdr.directory(tmp_path) / 'active.json'
    active = json.loads(active_path.read_text())
    active['consumed'] = False
    workflow_cli._write_json(active_path, active)
    monkeypatch.setattr(herdr.Client, 'call', lambda *a, **kw: pytest.fail('wait must not control Herdr'))
    monkeypatch.setattr(herdr.sys, 'argv', ['paper-herdr', 'wait', '--project', str(tmp_path), jobs[0].name])
    herdr.main()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['status'] == 'completed'
    assert json.loads(active_path.read_text())['consumed']


def test_persistent_role_binding_reuses_verified_host_and_rejects_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    monkeypatch.setattr(herdr, 'host_alive', lambda *a: True)
    config = {'session': 'test', 'binary': 'herdr'}
    root = herdr.directory(tmp_path) / 'roles/experiment'
    workflow_cli._write_json(root / 'binding.json', {'pane_id': 'w1:p8', 'generation': 'generation', 'session': 'test'})
    workflow_cli._write_json(root / 'ready.json', {'generation': 'generation'})
    calls = []
    def call(self, *args):
        calls.append(args)
        return {'pane': {'pane_id': 'w1:p8', 'agent': 'paper-agent-experiment', 'cwd': str(tmp_path)}}
    monkeypatch.setattr(herdr.Client, 'call', call)
    result = herdr.ensure_role(tmp_path, 'experiment', config, herdr.Client('test', 'herdr'))
    assert result['pane_id'] == 'w1:p8'
    assert calls == [('pane', 'get', 'w1:p8')]
    workflow_cli._write_json(root / 'ready.json', {'generation': 'other-generation'})
    with pytest.raises(herdr.HerdrError, match='unresolved'):
        herdr.ensure_role(tmp_path, 'experiment', config, herdr.Client('test', 'herdr'))
    assert all(c[:2] != ('pane', 'split') for c in calls)


def test_role_mismatch_rejected_before_task_creation(tmp_path, transport):
    with pytest.raises(ValueError, match='does not belong'):
        herdr.submit(tmp_path, 'experiment_plan', 'Run', role='paper_writer', project_goal='Goal')
    assert not (herdr.directory(tmp_path) / 'active.json').exists()


def test_role_host_claims_mailbox_before_completion_and_accepts_next_task(tmp_path, monkeypatch):
    import io
    from paper_agent.agent import herdr_role
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p9')
    root = herdr.directory(tmp_path) / 'roles/experiment'
    workflow_cli._write_json(root / 'binding.json', {
        'project': str(tmp_path), 'role': 'experiment', 'session': 'test',
        'binary': 'herdr', 'generation': 'one', 'pane_id': 'w1:p9'})
    first, second = 'a' * 32, 'b' * 32
    for task in (first, second):
        workflow_cli._write_json(herdr.directory(tmp_path) / 'tasks' / task / 'request.json',
                                 {'role': 'experiment', 'node': 'experiment_plan', 'instruction': task})
    workflow_cli._write_json(root / 'inbox.json', {'id': first})
    executed, calls = [], []
    def run_task(folder):
        assert not (root / 'inbox.json').exists()
        executed.append(folder.name)
        workflow_cli._write_json(folder / 'result.json', {'status': 'completed'})
        if folder.name == first:
            # Simulate main queuing immediately after the receipt appears.
            workflow_cli._write_json(root / 'inbox.json', {'id': second})
    monkeypatch.setattr(herdr_role, 'run_task', run_task)
    monkeypatch.setattr(herdr.Client, 'call', lambda self, *args: calls.append(args))
    monkeypatch.setattr(herdr_role.sys, 'stdin', io.StringIO('/quit\n'))
    monkeypatch.setattr(herdr_role.select, 'select',
                        lambda *args: ([], [], []) if len(executed) < 2 else ([herdr_role.sys.stdin], [], []))
    herdr_role.serve(root)
    assert executed == [first, second]
    assert any(c[:2] == ('agent', 'rename') for c in calls)
    assert any('paper-agent-experiment' in c for c in calls)
    assert not (root / 'ready.json').exists()


@pytest.mark.parametrize('node,role', [('data_search', 'idea_generator'), ('refine', 'idea_generator'),
    ('experiment_plan', 'experiment'), ('experiment_bridge', 'experiment'),
    ('paper_write', 'paper_writer'), ('improvement', 'paper_writer')])
def test_all_workflow_stages_route_to_assigned_herdr_role(tmp_path, monkeypatch, node, role):
    config = {'nodes': list(workflow_cli.WORKFLOW_NODE_NAMES)}
    monkeypatch.setattr(herdr, 'load_config', lambda p: config)
    monkeypatch.setattr(herdr, 'submit', lambda p, n, i, **kw: (n, kw['role']))
    assert stage_backend.dispatch_stage(tmp_path, node, 'Task', role=role) == (node, role)


def test_role_terminal_chat_keeps_separate_history_without_execution_tools(tmp_path, monkeypatch):
    from paper_agent.agent import herdr_role, model_client
    herdr.directory(tmp_path).mkdir(parents=True)
    calls = []
    class Model:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, tools):
            assert tools == []
            calls.append(messages)
            return ModelResponse(content='Discussion only')
    monkeypatch.setattr(model_client, 'ModelClient', Model)
    assert herdr_role.chat(tmp_path, 'paper_writer', 'First question') == 'Discussion only'
    herdr_role.chat(tmp_path, 'paper_writer', 'Second question')
    assert any(m.get('content') == 'First question' for m in calls[1])
    assert len(RoleSession(tmp_path, 'paper_writer', channel='discussion').load()['messages']) == 4
    assert RoleSession(tmp_path, 'paper_writer').load()['messages'] == []
    assert not (tmp_path / 'workflow-state.json').exists()


def test_role_terminal_chat_rejects_unreconciled_stage(tmp_path):
    from paper_agent.agent import herdr_role
    workflow_cli._write_json(herdr.directory(tmp_path) / 'active.json', {'id': 'a' * 32, 'consumed': False})
    with pytest.raises(herdr.HerdrError, match='Previous task'):
        herdr_role.chat(tmp_path, 'experiment', 'Can I change the experiment?')
