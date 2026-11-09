"""Exercise public advancement through real main dispatch, without network calls."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from paper_agent.agent import coordinator, workflow_cli
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.workflow import NodeResult, WorkflowConfig, WorkflowRunner, WorkflowState


def main_model(monkeypatch, node, role="idea_generator"):
    original = coordinator.CoordinatorAgent
    calls = []
    instruction = "Do the authorized stage"

    class Model:
        def complete(self, messages, tools):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(tool_calls=(ToolCall("stage", "delegate_stage", {
                    "assignment": {"node": node, "role": role, "instruction": instruction}}),))
            return ModelResponse(content="已核对产物。")

    def factory(*a, **kw):
        nonlocal instruction
        if kw.get("stage_task"):
            instruction = kw["stage_task"]["instruction"]
        return original(*a, model_client=Model(), **kw)
    monkeypatch.setattr(coordinator, "CoordinatorAgent", factory)
    return calls


def pending(root, *, stop_after=None):
    report = root / "literature/RESEARCH_REPORT.md"
    report.parent.mkdir()
    report.write_text("Unique original report body")
    saved = WorkflowState("test", project_goal="Original research goal", mode="expert",
                          status="awaiting_user", current_node="literature",
                          workflow_stop_after=stop_after, node_results={"literature": {}},
                          expert_checkpoint={"node": "literature", "status": "pending",
                              "report": "literature/RESEARCH_REPORT.md", "next_node": "data_search",
                              "stop_after": "literature"})
    workflow_cli._write_json(root / "workflow-state.json", saved.to_dict())
    return report


def stage_stub(monkeypatch, root, node):
    def execute(self, state, prompt):
        if node == "data_search":
            approved = json.loads((root / "workflow-state.json").read_text())
            assert approved["history"][-1]["event"] == "expert_approved"
        path = root / self._review_files(node)[0]
        path.parent.mkdir(exist_ok=True)
        path.write_text("Unique produced report body")
        checkpoint = root / self._checkpoint_path(node)
        checkpoint.write_text(json.dumps({"summary": "Ready", "questions": ["Scope?", "Budget?"]}))
        return NodeResult(path.read_text(), {"phase": node})
    monkeypatch.setattr(WorkflowRunner, "_run_" + node, execute)
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: SimpleNamespace(project=project))


def test_approval_freezes_files_then_main_dispatches_next_node(tmp_path, monkeypatch):
    report = pending(tmp_path)
    report.write_text("Unique user edited body")
    requests = main_model(monkeypatch, "data_search")
    stage_stub(monkeypatch, tmp_path, "data_search")
    state = workflow_cli.run_coordinated_workflow(tmp_path, continue_run=True, stop_after="data_search")
    assert len(requests) == 2
    assert state.status == "awaiting_user"
    assert state.current_node == "data_search"
    assert state.artifacts["literature"] == [{"path": "literature/RESEARCH_REPORT.md",
        "bytes": len(report.read_bytes()), "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}]
    serialized = (tmp_path / "workflow-state.json").read_text()
    assert "Unique user edited body" not in serialized
    assert "Unique produced report body" not in serialized
    assert "artifact" not in state.node_results["data_search"]


def test_failed_approval_never_invokes_main(tmp_path, monkeypatch):
    report = pending(tmp_path)
    report.write_text("")
    monkeypatch.setattr(coordinator, "CoordinatorAgent", lambda *a, **k: pytest.fail("must not invoke main"))
    with pytest.raises(ValueError, match="missing or empty"):
        workflow_cli.run_coordinated_workflow(tmp_path, continue_run=True)
    assert json.loads((tmp_path / "workflow-state.json").read_text())["status"] == "awaiting_user"


def test_failed_run_resume_is_dispatched_by_main(tmp_path, monkeypatch):
    saved = WorkflowState("failed", project_goal="Goal", current_node="literature", status="failed")
    workflow_cli._write_json(tmp_path / "workflow-state.json", saved.to_dict())
    requests = main_model(monkeypatch, "literature")
    stage_stub(monkeypatch, tmp_path, "literature")
    state = workflow_cli.run_coordinated_workflow(tmp_path, "Goal", start_node="literature", stop_after="literature")
    assert len(requests) == 2
    assert state.status == "completed"
    assert state.transitions == 1


def test_checkpoint_revision_is_assigned_by_main(tmp_path, monkeypatch):
    pending(tmp_path)
    requests = main_model(monkeypatch, "literature")
    stage_stub(monkeypatch, tmp_path, "literature")
    state = workflow_cli.run_coordinated_workflow(tmp_path, revise="Narrow the scope")
    assert len(requests) == 2
    assert state.status == "awaiting_user"
    assert state.expert_checkpoint["status"] == "pending"
    assert "Narrow the scope" in state.node_results["literature"]["instruction"]
    assert state.project_goal == "Original research goal"


def test_main_cannot_cross_stop_boundary(tmp_path, monkeypatch):
    requests = main_model(monkeypatch, "ideas")
    state = workflow_cli.run_coordinated_workflow(tmp_path, "Goal", stop_after="data_search")
    assert len(requests) == 2
    assert state.status == "failed"
    assert not state.node_results


def test_feedback_revision_returns_to_main_before_next_stage(tmp_path, monkeypatch):
    pending(tmp_path)
    stage_stub(monkeypatch, tmp_path, "literature")
    stage_stub(monkeypatch, tmp_path, "data_search")
    original = coordinator.CoordinatorAgent
    stages = []

    class Model:
        def complete(self, messages, tools):
            if len(stages) < 2:
                node = ("literature", "data_search")[len(stages)]
                if stages:
                    saved = json.loads((tmp_path / "workflow-state.json").read_text())
                    assert saved["current_node"] == "data_search"
                    assert saved["expert_checkpoint"]["status"] == "approved"
                stages.append(node)
                return ModelResponse(tool_calls=(ToolCall(node, "delegate_stage", {"assignment": {
                    "node": node, "role": "idea_generator", "instruction": "Apply feedback and verify evidence"}}),))
            return ModelResponse(content="修订并完成数据阶段。")

    monkeypatch.setattr(coordinator, "CoordinatorAgent",
                        lambda *a, **kw: original(*a, model_client=Model(), **kw))
    state = workflow_cli.run_coordinated_workflow(tmp_path, revise="Use only public datasets",
                                                advance_after_revision=True, stop_after="data_search")
    assert stages == ["literature", "data_search"]
    assert state.status == "awaiting_user"
    assert state.current_node == "data_search"
    assert state.transitions == 2


def test_first_delegation_keeps_goal_separate_from_stage_instruction(tmp_path, monkeypatch):
    main_model(monkeypatch, "literature")
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: SimpleNamespace(project=project))
    captured = []
    def literature(self, state, prompt):
        captured.append(prompt)
        report = tmp_path / "literature/RESEARCH_REPORT.md"
        report.parent.mkdir()
        report.write_text("Evidence")
        assert state.project_goal == "Unique overall research goal"
        assert prompt.count("Unique overall research goal") == 1
        assert prompt.count("Do the authorized stage") == 1
        assert "项目总目标" in prompt and "当前阶段指令" in prompt
        return NodeResult("literature/RESEARCH_REPORT.md")
    monkeypatch.setattr(WorkflowRunner, "_run_literature", literature)
    result = coordinator.CoordinatorAgent(tmp_path, stop_after="literature").go("Unique overall research goal")
    assert result.status == "completed"
    assert len(captured) == 1
    saved = json.loads((tmp_path / "workflow-state.json").read_text())
    assert saved["project_goal"] == "Unique overall research goal"
    assert saved["node_results"]["literature"]["instruction"] == "Do the authorized stage"
    assert "node_instructions" not in saved
    assert "instruction" not in saved["history"][0]
    # Goal is supplied once to main too, instead of both system and user text.
    session = json.loads((tmp_path / ".paper-agent/role-sessions/main.json").read_text())
    assert session["messages"][0]["content"] != "Unique overall research goal"


def test_runner_rejects_implicit_goal_replacement(tmp_path):
    state = WorkflowState("test", project_goal="Existing goal", current_node="literature")
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    with pytest.raises(ValueError, match="instruction"):
        runner.run("A stage instruction", state=state)
