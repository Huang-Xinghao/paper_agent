"""Web role snapshots and collaboration choices survive request boundaries."""
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paper_agent.api import app as web
from paper_agent.api import store
from paper_agent.agent import coordinator
from paper_agent.agent.runtime.sessions import RoleSession
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.storage import write_json


@pytest.fixture
def project_api(tmp_path, monkeypatch):
    root = tmp_path / 'projects'
    project = root / 'study'
    project.mkdir(parents=True)
    monkeypatch.setenv('PAPER_AGENT_PROJECT_ROOTS', str(root))
    monkeypatch.setenv('PAPER_AGENT_WEB_DATA', str(tmp_path / 'web'))
    monkeypatch.delenv('PAPER_AGENT_PASSWORD_HASH', raising=False)
    monkeypatch.setattr(web, 'terminal_projects', lambda: set())
    monkeypatch.setattr(web, 'reconcile', lambda: None)
    # Exercise request validation and durable scheduling without starting a model worker.
    monkeypatch.setattr(web.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    store.initialize()
    record = web.register('study', project)
    return TestClient(web.app), project, record['id']


@pytest.mark.parametrize('mode', ['expert', 'auto'])
@pytest.mark.parametrize('action', ['messages', 'resume', 'coordinate', 'revise'])
def test_request_inherits_saved_mode(project_api, mode, action):
    client, project, identifier = project_api
    write_json(project / 'workflow-state.json', {'mode': mode, 'status': 'interrupted',
               'current_node': 'literature', 'project_goal': 'Goal'})
    report = project / 'literature/RESEARCH_REPORT.md'
    report.parent.mkdir()
    report.write_text('Current evidence')
    body = {'prompt': 'Continue research'}
    path = f'/api/projects/{identifier}/messages'
    if action != 'messages':
        path = f'/api/projects/{identifier}/runs'
        body['action'] = action
        if action == 'revise':
            body['node_task'] = {'node': 'literature', 'target': 'report',
                'instruction': 'Revise report', 'files': ['literature/RESEARCH_REPORT.md']}
    response = client.post(path, json=body)
    assert response.status_code == 202, response.text
    options = json.loads(store.rows('SELECT options FROM runs')[0]['options'])
    assert options['mode'] == mode
    assert coordinator.CoordinatorAgent(project, model_client=object()).mode == mode


def test_new_web_project_defaults_to_expert_and_explicit_auto_is_honored(project_api):
    client, project, identifier = project_api
    response = client.post(f'/api/projects/{identifier}/messages', json={'prompt': 'Study batteries'})
    assert response.status_code == 202
    assert json.loads(store.rows('SELECT options FROM runs')[0]['options'])['mode'] == 'expert'
    with store.connection() as db:
        db.execute("UPDATE runs SET status='completed'")
    response = client.post(f'/api/projects/{identifier}/messages', json={'prompt': 'Study batteries', 'mode': 'auto'})
    assert response.status_code == 202
    assert json.loads(store.rows('SELECT options FROM runs ORDER BY created_at DESC')[0]['options'])['mode'] == 'auto'


def pending(project):
    report = project / 'literature/RESEARCH_REPORT.md'
    report.parent.mkdir(exist_ok=True)
    report.write_text('Evidence')
    write_json(project / 'workflow-state.json', {
        'mode': 'expert', 'status': 'awaiting_user', 'current_node': 'literature',
        'expert_checkpoint': {'status': 'pending', 'node': 'literature', 'report': 'literature/RESEARCH_REPORT.md',
                              'files': ['literature/RESEARCH_REPORT.md']}})


def test_switching_mode_cannot_bypass_pending_review(project_api):
    client, project, identifier = project_api
    pending(project)
    response = client.post(f'/api/projects/{identifier}/messages', json={'prompt': 'Continue', 'mode': 'auto'})
    assert response.status_code == 409
    assert store.rows('SELECT * FROM runs') == []


@pytest.mark.parametrize('outcome', ['completed', 'failed'])
def test_discussion_result_cannot_hide_pending_review(project_api, outcome):
    client, project, identifier = project_api
    pending(project)
    with store.connection() as db:
        db.execute('INSERT INTO runs(id,project_id,status,options,created_at) VALUES(?,?,?,?,?)',
                   ('discussion', identifier, outcome, '{"action":"coordinate"}', store.now()))
    response = client.get(f'/api/projects/{identifier}')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'awaiting_user'
    assert response.json()['agents'][0]['status'] == 'awaiting_user'


def test_role_handoff_and_refresh_show_actual_actor_without_stale_work(project_api):
    client, project, identifier = project_api
    session = RoleSession(project, 'experiment')
    data = session.load()
    data['tasks'] = [{'id': 'task-1', 'phase': 'experiment_plan', 'status': 'running',
                      'instruction': 'Design a controlled evaluation'}]
    session.save(data)
    with store.connection() as db:
        db.execute('INSERT INTO runs(id,project_id,status,options,created_at) VALUES(?,?,?,?,?)',
                   ('run-1', identifier, 'running', '{"action":"coordinate"}', store.now()))
    store.event(identifier, 'assignment_start', {'role': 'main', 'assignment': {
        'role': 'experiment', 'node': 'experiment_plan'}}, 'run-1')
    def views():
        return {item['role']: item for item in client.get(f'/api/projects/{identifier}').json()['agents']}
    value = views()
    assert value['experiment']['is_active'] and value['experiment']['instruction'] == 'Design a controlled evaluation'
    assert value['main']['status'] == 'waiting'
    store.event(identifier, 'assignment_end', {'role': 'experiment', 'status': 'completed'}, 'run-1')
    assert views()['main']['is_active']
    with store.connection() as db:
        db.execute("UPDATE runs SET status='interrupted'")
    value = views()
    assert not any(item['is_active'] for item in value.values())
    assert value['experiment']['status'] == 'interrupted'


def test_auto_main_can_dispatch_two_successive_stages(tmp_path, monkeypatch):
    from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls <= 2:
                return ModelResponse(tool_calls=(ToolCall(str(self.calls), 'delegate_stage', {'assignment': {
                    'node': ['literature', 'data_search'][self.calls - 1], 'role': 'idea_generator',
                    'instruction': 'Deliver verified evidence'}}),))
            return ModelResponse(content='Both stages delivered.')

    def execute(project, node, instruction, **options):
        class Agent:
            def __init__(self): self.project = project
            def go(self, prompt, *, phase, completion_check):
                output = 'literature/RESEARCH_REPORT.md' if phase == 'literature' else 'data-search/DATA_REPORT.md'
                path = project / output
                path.parent.mkdir(exist_ok=True)
                path.write_text('Verified evidence')
                completion_check()
                return SimpleNamespace(iterations=1)
        runner = WorkflowRunner(Agent(), config=WorkflowConfig(mode=options['mode']))
        state = runner.start_from_project(node, options['project_goal'])
        result = runner.run(options['project_goal'], state=state, stop_after=node)
        write_json(project / 'workflow-state.json', result.to_dict())
        return result

    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    model = Model()
    main = coordinator.CoordinatorAgent(tmp_path, model_client=model, mode='auto', project_goal='Goal')
    result = main.go('Deliver literature and data evidence')
    assert result.status == 'completed' and main.assignments == 2
    assert model.calls == 3


@pytest.mark.parametrize('backend', ['local', 'herdr'])
def test_runtime_task_identifies_terminal_role_without_web_events(project_api, monkeypatch, backend):
    client, project, identifier = project_api
    monkeypatch.setattr(web, 'terminal_projects', lambda: {str(project)})
    # A previous browser run must not identify the active terminal role.
    store.event(identifier, 'assistant_output', {'role': 'idea_generator'})
    root = project / '.paper-agent/herdr'
    task_id = 'a' * 32
    write_json(root / 'active.json', {'id': task_id, 'backend': backend, 'consumed': False})
    write_json(root / 'tasks' / task_id / 'request.json', {
        'id': task_id, 'project': str(project), 'role': 'paper_writer',
        'node': 'paper_write', 'instruction': 'Write the results section'})
    def views():
        return {item['role']: item for item in client.get(f'/api/projects/{identifier}').json()['agents']}
    value = views()
    assert value['paper_writer']['is_active']
    assert value['paper_writer']['instruction'] == 'Write the results section'
    assert value['paper_writer']['task_id'] == task_id
    write_json(root / 'active.json', {'id': task_id, 'backend': backend, 'consumed': True})
    value = views()
    assert not value['paper_writer']['is_active']
    assert value['main']['is_active']
    # Invalid task IDs are never resolved as paths outside the transport directory.
    write_json(root / 'active.json', {'id': '../outside', 'consumed': False})
    assert not views()['paper_writer']['is_active']
    monkeypatch.setattr(web, 'terminal_projects', lambda: set())
    assert not any(item['is_active'] for item in views().values())
