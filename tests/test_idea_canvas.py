"""Canvas saves must preserve proposal/workflow and reject lost updates."""

import importlib
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from paper_agent.api import store

api_module = importlib.import_module("paper_agent.api.app")


@pytest.fixture
def canvas(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "management"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(root))
    monkeypatch.delenv("PAPER_AGENT_PUBLIC_HOSTS", raising=False)
    with TestClient(api_module.app) as client:
        project = client.post("/api/projects", json={"name": "canvas-study"}).json()
        directory = root / "canvas-study"
        # create() may choose a generated directory; use the registered project path.
        from pathlib import Path

        directory = Path(project["path"])
        (directory / "refine-logs").mkdir(exist_ok=True)
        proposal = directory / "refine-logs/FINAL_PROPOSAL.md"
        proposal.write_text("# Original proposal\n", encoding="utf-8")
        endpoint = f"/api/projects/{project['id']}/idea-canvas"
        yield client, endpoint, proposal, project


def draft(proposal):
    return {
        "version": 1,
        "sourceHash": hashlib.sha256(proposal.read_bytes()).hexdigest(),
        "nodes": [
            {"id": "input", "position": {"x": 1, "y": 2}, "data": {"title": "输入"}},
            {"id": "model", "position": {"x": 3, "y": 4}, "data": {"title": "预测"}},
        ],
        "edges": [{"id": "edge", "source": "input", "target": "model"}],
        "changes": ["人工新增模块"],
        "review": {
            "sections": [{"id": "goal", "title": "研究目标", "summary": "人工修订目标", "details": "具体依据", "focus": True, "edited": True}],
            "feedback": "先验证简单方法",
        },
    }


def test_operation_progress_belongs_to_current_run_and_survives_reload(canvas):
    client, endpoint, _, project = canvas
    with store.connection() as db:
        db.execute("INSERT INTO runs(id,project_id,status,options,created_at,finished_at) VALUES(?,?,?,?,?,?)",
                   ('previous', project['id'], 'completed', '{"action":"proposal_revise"}', '2026-01-01', '2026-01-02'))
        db.execute("INSERT INTO runs(id,project_id,status,options,created_at) VALUES(?,?,?,?,?)",
                   ('current', project['id'], 'running', '{"action":"proposal_revise"}', '2026-01-03'))
    store.event(project['id'], 'proposal_revision_progress', {'stage': 'ready', 'receivedChars': 9000}, 'previous')
    operation = client.get(endpoint).json()['operation']
    assert operation['id'] == 'current' and 'progress' not in operation
    store.event(project['id'], 'proposal_revision_progress', {'stage': 'receiving', 'receivedChars': 260}, 'current')
    for _ in range(2):
        operation = client.get(endpoint).json()['operation']
        assert operation['createdAt'] == '2026-01-03' and operation['finishedAt'] is None
        assert operation['progress']['stage'] == 'receiving'
        assert operation['progress']['receivedChars'] == 260
    store.run_update('current', status='failed', finished_at='2026-01-04', error='Disconnected')
    operation = client.get(endpoint).json()['operation']
    assert operation['status'] == 'failed' and operation['finishedAt'] == '2026-01-04'


def test_save_roundtrip_preserves_original_and_rejects_concurrent_writer(canvas):
    client, endpoint, proposal, project = canvas
    before = proposal.read_bytes()
    state = client.get(f"/api/projects/{project['id']}").json()["state"]
    assert client.get(endpoint).json()["draft"] is None
    body = {"sha256": "", "draft": draft(proposal)}
    saved = client.put(endpoint, json=body)
    assert saved.status_code == 200
    loaded = client.get(endpoint).json()
    assert loaded["draft"]["nodes"][1]["data"]["title"] == "预测"
    assert loaded["draft"]["review"] == body["draft"]["review"]
    assert loaded["sha256"] == saved.json()["sha256"]
    assert client.put(endpoint, json=body).status_code == 409
    assert proposal.read_bytes() == before
    assert client.get(f"/api/projects/{project['id']}").json()["state"] == state
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM runs WHERE project_id=?", (project["id"],)
            ).fetchone()[0]
            == 0
        )


def test_source_update_and_dangling_edge_are_rejected(canvas):
    client, endpoint, proposal, _ = canvas
    body = {"sha256": "", "draft": draft(proposal)}
    body["draft"]["edges"][0]["target"] = "deleted"
    assert client.put(endpoint, json=body).status_code == 422
    body["draft"] = draft(proposal)
    proposal.write_text("# Updated by agent\n", encoding="utf-8")
    assert client.put(endpoint, json=body).status_code == 409
    assert not (proposal.parent / "IDEA_CANVAS.json").exists()


def test_review_validation_and_legacy_draft(canvas):
    client, endpoint, proposal, _ = canvas
    body = {"sha256": "", "draft": draft(proposal)}
    section = body["draft"]["review"]["sections"][0]
    body["draft"]["review"]["sections"].append(dict(section))
    assert client.put(endpoint, json=body).status_code == 422
    body["draft"]["review"]["sections"].pop()
    body["draft"]["review"]["feedback"] = "x" * 10001
    assert client.put(endpoint, json=body).status_code == 422
    del body["draft"]["review"]
    assert client.put(endpoint, json=body).status_code == 200
    assert client.get(endpoint).json()["draft"]["review"] is None


def test_running_project_blocks_save(canvas):
    client, endpoint, proposal, project = canvas
    with store.connection() as db:
        db.execute(
            "INSERT INTO runs(id, project_id, status, options, created_at) VALUES(?,?,?,?,?)",
            ("busy", project["id"], "running", json.dumps({}), store.now()),
        )
    assert (
        client.put(endpoint, json={"sha256": "", "draft": draft(proposal)}).status_code
        == 409
    )


def test_sidecar_symlink_cannot_escape_project(canvas, tmp_path):
    client, endpoint, proposal, _ = canvas
    outside = tmp_path / "outside.json"
    outside.write_text("do not change", encoding="utf-8")
    (proposal.parent / "IDEA_CANVAS.json").symlink_to(outside)
    assert (
        client.put(endpoint, json={"sha256": "", "draft": draft(proposal)}).status_code
        == 400
    )
    assert outside.read_text(encoding="utf-8") == "do not change"
