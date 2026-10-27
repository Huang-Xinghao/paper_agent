"""Web boundary and process-control tests, without live model/GPU calls."""

import importlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

from paper_agent.api import store

api_module = importlib.import_module("paper_agent.api.app")


@pytest.fixture
def web(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "management"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(root))
    monkeypatch.setattr(api_module, "route_message", lambda prompt, history, model: {
        "intent": "research", "research_prompt": prompt, "reply": "",
    })
    with TestClient(api_module.app) as client:
        yield client, root


def create(client):
    response = client.post("/api/projects", json={"name": "电池研究"})
    assert response.status_code == 201
    return response.json()


def seed_run(project_id, *, status="running", options=None):
    identifier = uuid.uuid4().hex
    with store.connection() as db:
        db.execute(
            "INSERT INTO runs(id,project_id,status,options,created_at,pid,pid_created) VALUES(?,?,?,?,?,?,?)",
            (
                identifier,
                project_id,
                status,
                json.dumps(options or {}),
                store.now(),
                os.getpid(),
                psutil.Process().create_time(),
            ),
        )
    (store.data_root() / "runs" / identifier).mkdir(parents=True)
    return identifier


def test_identity_reply_is_persisted_without_research(web, monkeypatch):
    from paper_agent.api.intent import route_message
    client, _ = web
    project = create(client)
    monkeypatch.setattr(api_module, "route_message", route_message)
    monkeypatch.setattr(api_module.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    endpoint = f"/api/projects/{project['id']}"
    response = client.post(endpoint + "/runs", json={"prompt": "你是谁？"})
    assert response.status_code == 200
    assert response.json()["intent"] == "chat"
    assert "Paper Agent" in response.json()["reply"]
    assert store.rows("SELECT * FROM runs") == []
    assert not (Path(project["path"]) / "workflow-state.json").exists()
    events = client.get(endpoint + "/activity").json()["events"]
    turn = next(e for e in events if e["kind"] == "conversation_turn")
    assert turn["data"]["prompt"] == "你是谁？"
    assert client.get(endpoint).json()["status"] == "idle"


def test_clarification_context_then_research(web, monkeypatch):
    client, _ = web
    project = create(client)
    endpoint = f"/api/projects/{project['id']}/runs"
    def route(prompt, history, model):
        if not history:
            return {"intent": "clarify", "reply": "你想研究什么主题？", "research_prompt": ""}
        assert history[-1]["prompt"] == "帮我做研究"
        assert history[-1]["reply"] == "你想研究什么主题？"
        return {"intent": "research", "research_prompt": "研究退役电池寿命预测", "reply": ""}
    monkeypatch.setattr(api_module, "route_message", route)
    first = client.post(endpoint, json={"prompt": "帮我做研究"})
    assert first.status_code == 200 and first.json()["intent"] == "clarify"
    assert not store.rows("SELECT * FROM runs")
    monkeypatch.setattr(api_module.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    second = client.post(endpoint, json={"prompt": "退役电池寿命预测"})
    assert second.status_code == 202
    options = json.loads(store.rows("SELECT options FROM runs")[0]["options"])
    assert options["prompt"] == "研究退役电池寿命预测"
    assert options["max_tool_rounds"] == 100


def test_routing_failure_never_starts_research(web, monkeypatch):
    client, _ = web
    project = create(client)
    def fail(*args):
        raise ValueError("bad model response")
    monkeypatch.setattr(api_module, "route_message", fail)
    response = client.post(f"/api/projects/{project['id']}/runs", json={"prompt": "一个问题"})
    assert response.status_code == 503
    assert not store.rows("SELECT * FROM runs")
    assert not store.rows("SELECT * FROM events WHERE kind='conversation_turn'")


def test_discover_existing_projects_without_modification(tmp_path, monkeypatch):
    root = tmp_path / "root"
    project = root / "existing"
    project.mkdir(parents=True)
    state = project / "workflow-state.json"
    state.write_text('{"status":"awaiting_results","task_prompt":"原始目标"}')
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "metadata"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(root))
    with TestClient(api_module.app) as client:
        items = client.get("/api/projects").json()
        assert len(items) == 1
        assert items[0]["state"]["task_prompt"] == "原始目标"
    assert state.read_text() == '{"status":"awaiting_results","task_prompt":"原始目标"}'


def test_path_boundary_and_private_files(web, tmp_path):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (root / "escape.md").symlink_to(outside)
    (root / ".env").write_text("API_KEY=secret")
    (root / "report.md").write_text("# report")
    identifier = project["id"]
    assert (
        client.get(
            f"/api/projects/{identifier}/file", params={"path": "../outside.md"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/api/projects/{identifier}/file", params={"path": ".env"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/api/projects/{identifier}/file", params={"path": "escape.md"}
        ).status_code
        == 404
    )
    assert [
        item["path"]
        for item in client.get(f"/api/projects/{identifier}/files").json()["files"]
    ] == ["report.md"]
    assert (
        client.post(
            "/api/projects", json={"name": "outside", "path": str(tmp_path)}
        ).status_code
        == 400
    )


def test_file_conflict_and_active_run_guard(web):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    report = root / "report.md"
    report.write_text("first")
    endpoint = f"/api/projects/{project['id']}/file"
    initial = client.get(endpoint, params={"path": "report.md"}).json()
    report.write_text("changed by CLI")
    assert (
        client.put(
            endpoint,
            params={"path": "report.md"},
            json={"content": "edit", "sha256": initial["sha256"]},
        ).status_code
        == 409
    )
    current = client.get(endpoint, params={"path": "report.md"}).json()
    saved = client.put(
        endpoint,
        params={"path": "report.md"},
        json={"content": "saved", "sha256": current["sha256"]},
    )
    assert saved.status_code == 200 and report.read_text() == "saved"
    seed_run(project["id"])
    assert (
        client.put(
            endpoint,
            params={"path": "report.md"},
            json={"content": "bad", "sha256": saved.json()["sha256"]},
        ).status_code
        == 409
    )
    assert report.read_text() == "saved"
    records = store.rows("SELECT * FROM events WHERE project_id=?", (project["id"],))
    assert any(row["kind"] == "file_saved" for row in records)


def test_review_uses_current_file_hashes(web, monkeypatch):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    (root / "report.md").write_text("review report")
    state = {
        "run_id": "r",
        "status": "awaiting_user",
        "current_node": "literature",
        "task_prompt": "goal",
        "expert_checkpoint": {
            "node": "literature",
            "status": "pending",
            "files": ["report.md"],
            "questions": ["Which prediction target?", "Which data source?"],
            "report": "report.md",
        },
    }
    (root / "workflow-state.json").write_text(json.dumps(state))
    token = client.get(f"/api/projects/{project['id']}").json()["checkpoint_token"]
    (root / "report.md").write_text("new report")
    endpoint = f"/api/projects/{project['id']}/runs"
    assert (
        client.post(
            endpoint, json={"action": "review", "checkpoint_token": token}
        ).status_code
        == 409
    )
    assert store.rows("SELECT * FROM runs") == []
    token = client.get(f"/api/projects/{project['id']}").json()["checkpoint_token"]
    monkeypatch.setattr(
        api_module.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=os.getpid(), wait=lambda: 0),
    )
    assert (
        client.post(
            endpoint,
            json={
                "action": "review",
                "checkpoint_token": token,
                "answer": "narrow the scope",
            },
        ).status_code
        == 202
    )
    stored = json.loads(store.rows("SELECT options FROM runs")[0]["options"])
    assert stored["answer"] == "narrow the scope"
    assert "mode" not in stored and "prompt" not in stored


def test_global_run_guard_and_stop(web):
    client, _ = web
    first = create(client)
    second = create(client)
    run_id = seed_run(first["id"])
    assert (
        client.post(
            f"/api/projects/{second['id']}/runs", json={"prompt": "goal"}
        ).status_code
        == 409
    )
    assert client.post(f"/api/projects/{first['id']}/stop").status_code == 202
    assert (store.data_root() / "runs" / run_id / "stop").exists()
    assert store.rows("SELECT status FROM runs")[0]["status"] == "stopping"


def test_dead_supervisor_reconciled_without_replaying_work(web):
    client, _ = web
    project = create(client)
    run_id = seed_run(project["id"])
    store.run_update(run_id, pid=2**30)
    data = client.get(f"/api/projects/{project['id']}").json()
    assert data["status"] == "interrupted"
    assert data["runs"][0]["finished_at"]


def test_input_validation_and_origin(web):
    client, _ = web
    project = create(client)
    endpoint = f"/api/projects/{project['id']}/runs"
    assert (
        client.post(
            endpoint, json={"prompt": "goal", "start_node": "unknown"}
        ).status_code
        == 422
    )
    assert (
        client.post(endpoint, json={"prompt": "goal", "timeout": -1}).status_code == 422
    )
    assert client.post(endpoint, json={"prompt": ""}).status_code == 400
    assert (
        client.post(
            "/api/projects",
            json={"name": "x"},
            headers={"origin": "https://evil.example"},
        ).status_code
        == 403
    )


def test_worker_revision_and_durable_events(web, monkeypatch):
    from paper_agent.api.worker import execute

    monkeypatch.setattr("paper_agent.api.runtime.check_runtime", lambda: None)
    from paper_agent.agent import workflow_cli

    client, _ = web
    project = create(client)
    root = Path(project["path"])
    (root / "report.md").write_text("report")
    state = {
        "status": "awaiting_user",
        "expert_checkpoint": {
            "node": "literature",
            "report": "report.md",
            "files": ["report.md"],
            "questions": ["Which prediction target?", "Which data source?"],
        },
    }
    (root / "workflow-state.json").write_text(json.dumps(state))
    token = client.get(f"/api/projects/{project['id']}").json()["checkpoint_token"]
    run_id = seed_run(
        project["id"],
        options={
            "action": "review",
            "answer": "focus on RUL",
            "checkpoint_token": token,
            "max_tool_rounds": 17,
        },
    )
    captured = {}

    def fake_workflow(path, **kwargs):
        captured.update(kwargs)
        kwargs["on_event"]({"kind": "tool_end", "tool": "read", "time": store.now()})
        return SimpleNamespace(status="awaiting_user", error=None)

    monkeypatch.setattr(workflow_cli, "run_workflow", fake_workflow)
    execute(run_id)
    assert captured["max_iterations"] == 17
    assert captured["advance_after_revision"] is True
    assert "Which prediction target?" in captured["revise"]
    assert "focus on RUL" in captured["revise"]
    assert "不得篡改执行回执" in captured["revise"]
    assert (
        store.rows("SELECT * FROM events WHERE run_id=?", (run_id,))[0]["kind"]
        == "tool_end"
    )
    assert (
        json.loads((store.data_root() / "runs" / run_id / "result.json").read_text())[
            "status"
        ]
        == "awaiting_user"
    )
    (root / "report.md").write_text("changed")
    with pytest.raises(ValueError, match="已变化"):
        execute(run_id)


def test_public_state_excludes_large_internal_artifacts(web):
    client, _ = web
    project = create(client)
    state = {
        "status": "completed",
        "task_prompt": "goal",
        "artifacts": {"literature": "internal content" * 10000},
        "history": [
            {
                "node": "literature",
                "timestamp": store.now(),
                "metadata": {"large": "data" * 10000},
            }
        ],
    }
    target = Path(project["path"]) / "workflow-state.json"
    original = json.dumps(state)
    target.write_text(original)
    result = client.get(f"/api/projects/{project['id']}")
    assert result.status_code == 200
    public = result.json()["state"]
    assert "artifacts" not in public
    assert "metadata" not in public["history"][0]
    assert public["history"][0]["node"] == "literature"
    assert len(result.content) < 3000
    assert target.read_text() == original


def test_owner_login_protects_all_api_and_revokes_session(web, monkeypatch):
    from paper_agent.api import auth
    client, _ = web
    monkeypatch.setenv("PAPER_AGENT_PASSWORD_HASH", auth.password_hash("test-password"))
    monkeypatch.setenv("PAPER_AGENT_COOKIE_SECURE", "0")
    headers = {"Origin": "http://localhost"}
    assert client.get("/api/auth/session").json() == {"enabled": True, "authenticated": False}
    for path in ["/api/health", "/api/projects", "/api/docs", "/api/openapi.json", "/api/projects/unknown/events", "/api/projects/unknown/download"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "test-password"}).status_code == 403
    assert client.post("/api/auth/login", headers={"Origin": "https://evil.example"}, json={"username": "admin", "password": "test-password"}).status_code == 403
    assert client.post("/api/auth/login", headers=headers, json={"username": "admin", "password": "wrong"}).status_code == 401
    response = client.post("/api/auth/login", headers=headers, json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert client.get("/api/health").status_code == 200
    token = client.cookies.get(auth.COOKIE)
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/api/health").status_code == 401


def test_owner_login_rate_limit_and_expiration(web, monkeypatch):
    import time
    from paper_agent.api import auth
    client, _ = web
    monkeypatch.setenv("PAPER_AGENT_PASSWORD_HASH", auth.password_hash("test-password"))
    headers = {"Origin": "http://localhost"}
    for _ in range(10):
        assert client.post("/api/auth/login", headers=headers, json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", headers=headers, json={"username": "admin", "password": "wrong"}).status_code == 429
    token = "expired"
    import hashlib
    with store.connection() as db:
        db.execute("INSERT INTO web_sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() - 1))
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/api/health").status_code == 401


def test_resume_can_switch_to_expert_mode_without_changing_saved_state(web, monkeypatch):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    state = {"task_prompt": "goal", "status": "completed", "mode": "auto", "current_node": "paper_compile"}
    state_path = root / "workflow-state.json"
    original = json.dumps(state)
    state_path.write_text(original)
    monkeypatch.setattr(api_module.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    response = client.post(f"/api/projects/{project['id']}/runs", json={"action": "resume", "mode": "expert", "start_node": "paper_compile"})
    assert response.status_code == 202
    options = json.loads(store.rows("SELECT options FROM runs")[0]["options"])
    assert options["mode"] == "expert"
    assert options["start_node"] == "paper_compile"
    assert state_path.read_text() == original


def test_resume_review_timeout_is_forwarded_and_validated(web, monkeypatch):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    (root / "workflow-state.json").write_text(json.dumps({
        "status": "failed", "failed_node": "improvement", "task_prompt": "goal"}))
    monkeypatch.setattr(api_module.subprocess, "Popen", lambda *a, **kw:
                        SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    endpoint = f"/api/projects/{project['id']}/runs"
    for field in ("paper_review_timeout", "paper_review_round2_timeout"):
        for timeout in (0, -1, 14401):
            assert client.post(endpoint, json={"action": "resume", field: timeout}).status_code == 422
    response = client.post(endpoint, json={"action": "resume", "paper_review_timeout": 5400,
                                          "paper_review_round2_timeout": 600})
    assert response.status_code == 202
    options = json.loads(store.rows("SELECT options FROM runs")[0]["options"])
    assert options["paper_review_timeout"] == 5400
    assert options["paper_review_round2_timeout"] == 600
    assert options["start_node"] == "improvement"


def test_activity_pages_are_project_scoped_and_recover_forward_events(web):
    client, _ = web
    project = create(client)
    other = client.post('/api/projects', json={'name': 'other'}).json()
    for index in range(5):
        store.event(project['id'], 'assistant_output', {'content': f'output {index}'})
        store.event(other['id'], 'assistant_output', {'content': 'other project'})
    path = f"/api/projects/{project['id']}/activity"
    latest = client.get(path, params={'limit': 2}).json()
    assert latest['has_more'] is True
    assert [event['data']['content'] for event in latest['events']] == ['output 3', 'output 4']
    before = latest['events'][0]['id']
    older = client.get(path, params={'limit': 2, 'before': before}).json()
    assert [event['data']['content'] for event in older['events']] == ['output 1', 'output 2']
    forward = client.get(path, params={'limit': 2, 'after': older['events'][-1]['id']}).json()
    assert forward['events'] == latest['events']
    assert forward['has_more'] is False
    assert client.get(path, params={'before': before, 'after': 0}).status_code == 400
    assert client.get('/api/projects/unknown/activity').status_code == 404


def test_archived_project_stays_out_of_list_after_rediscovery(web):
    client, _ = web
    project = create(client)
    state = Path(project["path"]) / "workflow-state.json"
    content = '{"status":"failed"}'
    state.write_text(content)
    with store.connection() as db:
        db.execute("INSERT INTO archived_projects VALUES (?, ?)", (project["id"], store.now()))
    api_module.register(project["name"], project["path"])
    assert client.get("/api/projects").json() == []
    assert state.read_text() == content
    assert client.get(f"/api/projects/{project['id']}").status_code == 200


@pytest.mark.parametrize("requested,expected", [(None, 100), (140, 140)])
def test_review_round_budget_does_not_restore_old_default(web, monkeypatch, requested, expected):
    client, _ = web
    project = create(client)
    root = Path(project["path"])
    (root / "report.md").write_text("report")
    (root / "workflow-state.json").write_text(json.dumps({
        "status": "awaiting_user",
        "expert_checkpoint": {"node": "literature", "report": "report.md", "files": ["report.md"]},
    }))
    seed_run(project["id"], status="completed", options={"max_tool_rounds": 30, "timeout": 600})
    token = client.get(f"/api/projects/{project['id']}").json()["checkpoint_token"]
    monkeypatch.setattr(api_module.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    body = {"action": "review", "checkpoint_token": token}
    if requested is not None:
        body["max_tool_rounds"] = requested
    response = client.post(f"/api/projects/{project['id']}/runs", json=body)
    assert response.status_code == 202
    options = json.loads(store.rows("SELECT options FROM runs WHERE id=?", (response.json()["id"],))[0]["options"])
    assert options["max_tool_rounds"] == expected
    assert options["timeout"] == 600


def test_worker_forwards_separate_review_budgets(web, monkeypatch):
    from paper_agent.api import worker, runtime
    from paper_agent.agent import workflow_cli
    client, _ = web
    project = create(client)
    run_id = seed_run(project['id'], options={
        'action': 'resume', 'start_node': 'improvement', 'prompt': 'goal',
        'paper_review_timeout': 1800, 'paper_review_round2_timeout': 1200,
    })
    captured = {}
    monkeypatch.setattr(runtime, 'check_runtime', lambda: None)
    def run(path, **options):
        captured.update(options)
        return SimpleNamespace(status='completed', error=None)
    monkeypatch.setattr(workflow_cli, 'run_workflow', run)
    worker.execute(run_id)
    assert captured['paper_review_timeout'] == 1800
    assert captured['paper_review_round2_timeout'] == 1200
    assert captured['start_node'] == 'improvement'


def test_review_progress_includes_current_elapsed_time(web):
    from datetime import datetime, timezone, timedelta
    client, _ = web
    project = create(client)
    seed_run(project['id'])
    (Path(project['path']) / 'workflow-progress.json').write_text(json.dumps({
        'improvement': {'round': 2, 'stage': 'review', 'timeout_seconds': 1200,
                        'stage_started_at': (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()}
    }))
    progress = client.get(f"/api/projects/{project['id']}").json()['progress']['improvement']
    assert progress['round'] == 2
    assert 25 <= progress['elapsed_seconds'] < 35


def test_completed_project_message_dispatches_scoped_task(web, monkeypatch):
    client, _ = web
    project = create(client)
    root = Path(project['path'])
    state = '{"status":"completed","current_node":"improvement","task_prompt":"study"}'
    (root / 'workflow-state.json').write_text(state)
    (root / 'figures').mkdir()
    (root / 'figures/figure_1.pdf').write_bytes(b'%PDF-original')
    monkeypatch.setattr(api_module, 'dispatch_message', lambda *a: {
        'tool': 'revise_artifact', 'arguments': {
            'node': 'paper_figure', 'target': '图一', 'instruction': '只改图一',
            'files': ['figures/figure_1.pdf'], 'compile_pdf': False}})
    monkeypatch.setattr(api_module.subprocess, 'Popen', lambda *a, **k:
                        SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    response = client.post(f"/api/projects/{project['id']}/messages", json={'prompt': '只改图一'})
    assert response.status_code == 202, response.text
    options = json.loads(store.rows('SELECT options FROM runs')[0]['options'])
    assert options['action'] == 'revise'
    assert options['node_task']['node'] == 'paper_figure'
    assert options['prompt'] == '只改图一'
    assert 'start_node' not in options
    assert (root / 'workflow-state.json').read_text() == state


def test_running_project_accepts_questions_but_rejects_new_execution(web, monkeypatch):
    client, _ = web
    project = create(client)
    (Path(project['path']) / 'workflow-state.json').write_text('{"status":"running"}')
    seed_run(project['id'])
    endpoint = f"/api/projects/{project['id']}/messages"
    monkeypatch.setattr(api_module, 'dispatch_message', lambda *a: {'tool': 'reply', 'reply': '正在生成论文。'})
    assert client.post(endpoint, json={'prompt': '现在进度？'}).json()['reply'] == '正在生成论文。'
    monkeypatch.setattr(api_module, 'dispatch_message', lambda *a: {'tool': 'resume_workflow', 'arguments': {}})
    assert client.post(endpoint, json={'prompt': '继续'}).status_code == 409
    assert len(store.rows('SELECT * FROM runs')) == 1


def test_router_failure_does_not_start_a_task(web, monkeypatch):
    client, _ = web
    project = create(client)
    def fail(*a):
        raise ValueError('invalid tool')
    monkeypatch.setattr(api_module, 'dispatch_message', fail)
    response = client.post(f"/api/projects/{project['id']}/messages", json={'prompt': '改图一'})
    assert response.status_code == 503
    assert store.rows('SELECT * FROM runs') == []


def test_worker_scoped_task_bypasses_workflow_and_gpu_check(web, monkeypatch):
    from paper_agent.api import worker, runtime
    from paper_agent.agent import node_task, workflow_cli
    client, _ = web
    project = create(client)
    run_id = seed_run(project['id'], options={
        'action': 'revise', 'prompt': '只改图一', 'max_tool_rounds': 12,
        'node_task': {'node': 'paper_figure', 'target': '图一', 'instruction': '只改图一',
                      'files': ['figures/figure_1.pdf']}})
    monkeypatch.setattr(runtime, 'check_runtime', lambda: pytest.fail('no GPU check for local edit'))
    monkeypatch.setattr(workflow_cli, 'run_workflow', lambda *a, **k: pytest.fail('must not run workflow'))
    captured = {}
    def revise(path, task, prompt, folder, **options):
        captured.update(task=task, prompt=prompt, **options)
        return SimpleNamespace(status='completed', error=None)
    monkeypatch.setattr(node_task, 'run_node_task', revise)
    worker.execute(run_id)
    assert captured['task']['node'] == 'paper_figure'
    assert captured['prompt'] == '只改图一'
    assert captured['max_iterations'] == 12
