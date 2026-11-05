"""Real registry/API/runtime boundaries with no model, shell or GPU requests."""
import importlib
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paper_agent import skill_registry as registry
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.api import store, worker
from paper_agent.tools.core import ToolContext
from paper_agent.tools.execution import skill, skill_resource_read

api = importlib.import_module("paper_agent.api.app")


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("PAPER_AGENT_WEB_DATA", str(tmp_path / "management"))
    monkeypatch.setenv("PAPER_AGENT_PROJECT_ROOTS", str(root))
    monkeypatch.delenv("PAPER_AGENT_PUBLIC_HOSTS", raising=False)
    monkeypatch.delenv("PAPER_AGENT_PASSWORD_HASH", raising=False)
    monkeypatch.setattr(api, "terminal_projects", lambda: set())
    with TestClient(api.app) as client:
        yield client


def document(**changes):
    return {"title": "证据检查", "description": "检查研究证据与目标是否一致", "instructions": "## 检查方法\n优先验证划分证据，不推测实验结果。",
            "enabled": True, "phases": ["research_refine"], **changes}


def create(client, **changes):
    response = client.post("/api/skills", json=document(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def update(client, item, **changes):
    response = client.put(f"/api/skills/{item['id']}", json={
        **{key: item[key] for key in document()}, **changes, "expected_etag": item["etag"]})
    return response


def test_create_update_conflict_restore_delete_and_export(workbench):
    client = workbench
    item = create(client)
    endpoint = f"/api/skills/{item['id']}"
    assert item["source"] == "custom" and item["version"] == 1
    assert "instructions" not in client.get("/api/skills").json()["items"][-1]
    updated = update(client, item, instructions="第二版方法").json()
    assert updated["version"] == 2
    stale = update(client, item, instructions="过期页面覆盖")
    assert stale.status_code == 409
    assert client.get(endpoint).json()["instructions"] == "第二版方法"
    assert [entry["version"] for entry in client.get(endpoint + "/history").json()["items"]] == [2, 1]
    restored = client.post(endpoint + "/restore", json={"version": 1, "expected_etag": updated["etag"]}).json()
    assert restored["version"] == 3 and restored["instructions"] == item["instructions"]
    exported = client.get(endpoint + "/download")
    assert exported.status_code == 200 and item["instructions"] in exported.text
    assert f"name: {item['id']}" in exported.text
    assert client.request("DELETE", endpoint, json={"expected_etag": item["etag"]}).status_code == 409
    assert client.request("DELETE", endpoint, json={"expected_etag": restored["etag"]}).status_code == 200
    assert client.get(endpoint).status_code == 404
    assert registry.run_snapshot("legacy-task")["skills"].get(item["id"]) is None


def test_builtin_customization_and_reset_preserve_git_file(workbench):
    client = workbench
    path = registry.BUILTIN_ROOT / "research-refine/SKILL.md"
    original = path.read_bytes()
    item = client.get("/api/skills/research-refine").json()
    assert item["required_phases"] == ["research_refine"] and item["version"] == 0
    assert update(client, item, enabled=False).status_code == 400
    assert update(client, item, phases=[]).status_code == 400
    changed = update(client, item, instructions="保留目标，细化方法。新增核查规则。").json()
    assert changed["customized"] and changed["version"] == 1
    assert path.read_bytes() == original
    reset = client.post("/api/skills/research-refine/restore", json={"version": 0, "expected_etag": changed["etag"]}).json()
    assert reset["version"] == 2 and not reset["customized"]
    assert reset["instructions"] == item["instructions"] and path.read_bytes() == original
    assert client.request("DELETE", "/api/skills/research-refine", json={"expected_etag": reset["etag"]}).status_code == 400


@pytest.mark.parametrize("changes", [{"title": "   "}, {"phases": ["unknown-phase"]}, {"instructions": ""}])
def test_invalid_skill_is_never_saved(workbench, changes):
    before = len(workbench.get("/api/skills").json()["items"])
    assert workbench.post("/api/skills", json=document(**changes)).status_code in {400, 422}
    assert len(workbench.get("/api/skills").json()["items"]) == before


def test_snapshot_keeps_body_resources_and_bindings_consistent(workbench, tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    (root / "research-refine/templates").mkdir(parents=True)
    (root / "research-refine/SKILL.md").write_text("---\nname: research-refine\ndescription: test\n---\nold builtin", encoding="utf-8")
    template = root / "research-refine/templates/layout.tex"
    template.write_text("old template", encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_ROOT", root)
    item = create(workbench)
    snapshot = registry.capture_snapshot()
    update(workbench, item, instructions="new method", enabled=False, phases=["paper_write"])
    template.write_text("new template", encoding="utf-8")
    (root / "research-refine/SKILL.md").write_text("changed builtin", encoding="utf-8")
    assert registry.phase_skills(snapshot, "research_refine")[-1]["instructions"] == item["instructions"]
    assert registry.phase_skills(snapshot, "proposal_revision") == []
    ctx = ToolContext(tmp_path, skill_snapshot=snapshot)
    assert item["instructions"] in skill(ctx, item["id"]).output
    assert skill_resource_read(ctx, "research-refine", "templates/layout.tex").output == "old template"
    assert skill_resource_read(ctx, "research-refine", "../secret").is_error
    fresh = registry.capture_snapshot()
    assert skill(ToolContext(tmp_path, skill_snapshot=fresh), item["id"]).is_error


def test_real_agent_loads_bound_skill_and_tool_reads_same_frozen_version(workbench, tmp_path):
    item = create(workbench, phases=["literature"], instructions="UNIQUE_RULE_FROM_USER_V1")
    events = []
    class Model:
        def complete(self, messages, tools):
            assert "UNIQUE_RULE_FROM_USER_V1" in messages[0]["content"]
            assert "research-lit" in messages[0]["content"]
            assert "UNIQUE_RULE_FROM_USER_V2" not in messages[0]["content"]
            assert "bash" not in {entry["function"]["name"] for entry in tools}
            if len(messages) == 2:
                update(workbench, item, instructions="UNIQUE_RULE_FROM_USER_V2")
                return ModelResponse(tool_calls=(ToolCall("read-skill", "skill", {"name": item["id"]}),))
            assert "UNIQUE_RULE_FROM_USER_V1" in messages[-1]["content"]
            return ModelResponse(content="done")
    with registry.use_snapshot(registry.capture_snapshot()):
        agent = PhaseAgent(tmp_path / "research", phase="literature", model_client=Model(),
                           on_progress=lambda kind, data: events.append({"kind": kind, **data}))
        agent.go("check", completion_check=lambda: None)
    loaded = next(event for event in events if event["kind"] == "skills_loaded")
    used = next(entry for entry in loaded["skills"] if entry["id"] == item["id"])
    assert used["version"] == 1 and used["content_hash"] == item["content_hash"]
    assert registry.catalog()[item["id"]]["version"] == 2


def test_submission_defers_snapshot_until_phase_and_reuses_it_after_worker_restart(workbench, monkeypatch):
    client = workbench
    item = create(client, phases=["literature"])
    monkeypatch.setattr(api, "route_message", lambda *args: {"intent": "research", "research_prompt": "research", "reply": ""})
    monkeypatch.setattr(api.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=os.getpid(), wait=lambda: 0))
    project = client.post("/api/projects", json={"name": "snapshot"}).json()
    response = client.post(f"/api/projects/{project['id']}/runs", json={"prompt": "research"})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert registry.run_snapshot(run_id)["policy"] == "phase_start"
    update(client, item, instructions="updated after submission")
    called = []
    def execute(identifier):
        called.append(identifier)
        assert registry.snapshot_for_phase("literature")["skills"][item["id"]]["instructions"] == "updated after submission"
    monkeypatch.setattr(worker, "_execute", execute)
    worker.execute(run_id)
    assert called == [run_id]
    assert registry.current_snapshot()["skills"][item["id"]]["version"] == 2
    fresh = registry.catalog()[item["id"]]
    registry.save({**fresh, "instructions": "updated again"}, identifier=item["id"], expected_etag=fresh["etag"])
    worker.execute(run_id)
    with registry.use_run(run_id):
        assert registry.snapshot_for_phase("experiment_plan")["skills"][item["id"]]["version"] == 3


def test_same_agent_freezes_active_phase_but_refreshes_next_phase(workbench, tmp_path):
    item = create(workbench, phases=["literature", "experiment_plan"], instructions="rule-v1")
    calls = []
    class Model:
        def complete(self, messages, tools):
            calls.append(messages[0]["content"])
            return ModelResponse(content="done")
    agent = PhaseAgent(tmp_path / "project", model_client=Model())
    agent.go("first phase", phase="literature", completion_check=lambda: None)
    update(workbench, item, instructions="rule-v2")
    agent.go("same phase again", phase="literature", completion_check=lambda: None)
    agent.go("next phase", phase="experiment_plan", completion_check=lambda: None)
    assert "rule-v1" in calls[0] and "rule-v1" in calls[1] and "rule-v2" not in calls[1]
    assert "rule-v2" in calls[2] and "rule-v1" not in calls[2]


def test_legacy_run_keeps_its_submission_snapshot(workbench):
    item = create(workbench)
    old = registry.capture_snapshot()
    with store.connection() as db:
        db.execute("INSERT INTO skill_run_snapshots VALUES(?,?)", ("old-run", json.dumps(old)))
    update(workbench, item, instructions="newer")
    with registry.use_run("old-run"):
        assert registry.snapshot_for_phase("research_refine")["skills"][item["id"]]["version"] == 1


def test_builtin_release_update_preserves_custom_baseline_and_templates(workbench, tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    skill_root = root / "research-refine"
    (skill_root / "templates").mkdir(parents=True)
    (root / "release.json").write_text(json.dumps({"version": "1.0.0"}))
    (skill_root / "SKILL.md").write_text("builtin-old", encoding="utf-8")
    template = skill_root / "templates/example.txt"
    template.write_text("template-old", encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_ROOT", root)
    item = workbench.get("/api/skills/research-refine").json()
    assert item["builtin_release"]["version"] == "1.0.0"
    customized = update(workbench, item, instructions="my-custom").json()
    assert customized["base_release"] == item["builtin_release"]
    (root / "release.json").write_text(json.dumps({"version": "1.1.0"}))
    (skill_root / "SKILL.md").write_text("builtin-new", encoding="utf-8")
    template.write_text("template-new", encoding="utf-8")
    newer = workbench.get("/api/skills/research-refine").json()
    assert newer["instructions"] == "my-custom" and newer["update_available"]
    assert newer["base_release"]["version"] == "1.0.0" and newer["builtin_release"]["version"] == "1.1.0"
    assert update(workbench, customized, instructions="stale-save").status_code == 409
    snapshot = registry.capture_snapshot()
    assert snapshot["skills"]["research-refine"]["resources"]["templates/example.txt"] == "template-old"
    releases = workbench.get("/api/skills/research-refine/releases").json()["items"]
    assert [r["builtin_release"]["version"] for r in releases] == ["1.1.0", "1.0.0"]
    assert releases[0]["instructions"] == "builtin-new" and "resources" not in releases[0]
    reset = workbench.post("/api/skills/research-refine/restore", json={"version": 0, "expected_etag": newer["etag"]}).json()
    assert reset["follows_builtin"] and not reset["customized"] and not reset["update_available"]
    assert reset["instructions"] == "builtin-new"
    assert registry.capture_snapshot()["skills"]["research-refine"]["resources"]["templates/example.txt"] == "template-new"
    # 恢复内置是重新选择跟随发布，不是把当时那份正文复制后永久停住。
    (root / "release.json").write_text(json.dumps({"version": "1.2.0"}))
    (skill_root / "SKILL.md").write_text("builtin-newest", encoding="utf-8")
    following = workbench.get("/api/skills/research-refine").json()
    assert following["instructions"] == "builtin-newest" and following["version"] == reset["version"]
    assert following["base_release"]["version"] == "1.2.0" and following["follows_builtin"]
    # 恢复定制历史时，正文和资源基准一并恢复到旧发布版。
    old_custom = workbench.post("/api/skills/research-refine/restore", json={"version": 1, "expected_etag": following["etag"]}).json()
    assert old_custom["base_release"]["version"] == "1.0.0" and old_custom["update_available"]
    assert registry.capture_snapshot()["skills"]["research-refine"]["resources"]["templates/example.txt"] == "template-old"


def test_builtin_without_customization_follows_update_even_if_version_number_not_bumped(workbench, tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    (root / "research-refine/templates").mkdir(parents=True)
    (root / "release.json").write_text(json.dumps({"version": "1.0.0"}))
    (root / "research-refine/SKILL.md").write_text("original", encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_ROOT", root)
    item = workbench.get("/api/skills/research-refine").json()
    (root / "research-refine/templates/new.txt").write_text("new template", encoding="utf-8")
    newer = workbench.get("/api/skills/research-refine").json()
    assert newer["builtin_release"]["id"] != item["builtin_release"]["id"]
    assert newer["follows_builtin"] and not newer["customized"]
    assert newer["builtin_release"]["version"] == "1.0.0"


def test_skill_api_uses_existing_login_boundary(workbench, monkeypatch):
    monkeypatch.setenv("PAPER_AGENT_PASSWORD_HASH", "scrypt$unused$unused")
    assert workbench.get("/api/skills").status_code == 401
    assert workbench.post("/api/skills", json=document(), headers={"Origin": "http://127.0.0.1:5173"}).status_code == 401
