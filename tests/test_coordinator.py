import copy
import json
from types import SimpleNamespace

import pytest

from paper_agent.agent import coordinator
from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.role_sessions import RoleSession
from paper_agent.agent.workflow import WorkflowState


class Model:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        step = next(self.steps)
        return step(messages) if callable(step) else step


def call(name, **args):
    return ModelResponse(tool_calls=(ToolCall(name, name, args),))


def assignment(node, role="idea_generator"):
    return {"node": node, "role": role, "instruction": f"Complete only {node}"}


def stub_stages(monkeypatch, root, status="completed"):
    calls = []
    def execute(project, node, prompt, **options):
        calls.append((node, options))
        path = root / ("literature/RESEARCH_REPORT.md" if node == "literature" else "data-search/DATA_REPORT.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Measured evidence from {node}")
        return WorkflowState("stub", current_node=node, status=status, error="blocked" if status == "failed" else None,
                               node_results={node: {"artifact": str(path.relative_to(root))}}, expert_checkpoint=None)
    monkeypatch.setattr(coordinator, "execute_stage", execute)
    return calls


def test_serial_delegate_read_result_and_continue(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    def inspect(messages):
        result = json.loads(messages[-1]["content"])
        assert result["status"] == "completed"
        assert "literature/RESEARCH_REPORT.md" in result["artifacts"]
        assert "artifact" not in result["result"]
        assert result["artifact_handoff"][0]["sha256"]
        return call("read_project_file", path="literature/RESEARCH_REPORT.md")
    def continue_after_read(messages):
        assert "Measured evidence from literature" in messages[-1]["content"]
        return call("delegate_stage", assignment=assignment("data_search"))
    model = Model(call("delegate_stage", assignment=assignment("literature")), inspect,
                  continue_after_read, ModelResponse(content="Two stages completed."))
    result = CoordinatorAgent(tmp_path, model_client=model).go("Study literature and data")
    assert result.status == "completed"
    assert [node for node, _ in calls] == ["literature", "data_search"]
    session = RoleSession(tmp_path, "main").load()
    assert session["tasks"][-1]["final"] == "Two stages completed."
    assert session["session_id"] == result.session_id


def test_coordinator_receipt_excludes_full_report_and_tool_transcript():
    value = {"artifact": "Full report" * 20000, "tool_calls": ["read"] * 1000,
             "node_instructions": "long task" * 1000, "role": "idea_generator",
             "task_id": "verified-task", "iterations": 12}
    receipt = CoordinatorAgent._node_receipt(value)
    assert receipt == {"role": "idea_generator", "task_id": "verified-task", "iterations": 12}


@pytest.mark.parametrize("status", ["failed", "awaiting_results", "awaiting_user", "interrupted"])
def test_noncompleted_worker_blocks_further_execution(tmp_path, monkeypatch, status):
    calls = stub_stages(monkeypatch, tmp_path, status)
    model = Model(call("delegate_stage", assignment=assignment("literature")),
                  call("delegate_stage", assignment=assignment("data_search")),
                  ModelResponse(content="Stopped with evidence."))
    result = CoordinatorAgent(tmp_path, model_client=model).go("Study this topic")
    assert result.status == status
    assert len(calls) == 1


def test_existing_user_checkpoint_cannot_be_bypassed(tmp_path, monkeypatch):
    (tmp_path / "workflow-state.json").write_text('{"status":"awaiting_user","current_node":"refine"}')
    calls = stub_stages(monkeypatch, tmp_path)
    result = CoordinatorAgent(tmp_path, model_client=Model(
        call("delegate_stage", assignment=assignment("literature")),
        ModelResponse(content="Please approve the checkpoint."))).go("Continue")
    assert result.status == "awaiting_user" and calls == []


def test_explicit_stage_is_enforced_and_stops_after_one(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    task = assignment("literature")
    agent = CoordinatorAgent(tmp_path, model_client=Model(
        call("delegate_stage", assignment=task),
        call("delegate_stage", assignment=assignment("data_search")),
        ModelResponse(content="Only literature completed.")), stage_task=task)
    result = agent.go("Do literature only")
    assert len(calls) == 1
    assert result.status == "failed"  # Attempted expansion is reported, never executed.


def test_multiple_assignments_in_one_response_execute_nothing(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    response = ModelResponse(tool_calls=(
        ToolCall("a", "delegate_stage", {"assignment": assignment("literature")}),
        ToolCall("b", "delegate_stage", {"assignment": assignment("data_search")}),
    ))
    result = CoordinatorAgent(tmp_path, model_client=Model(response, ModelResponse(content="Stopped"))).go("Research")
    assert calls == [] and result.status == "failed"


def test_main_conversation_resumes_and_decision_is_answered(tmp_path):
    first = CoordinatorAgent(tmp_path, model_client=Model(
        call("request_user_decision", question="Which research direction?"),
        ModelResponse(content="Please choose a direction."))).go("Help decide")
    assert first.status == "awaiting_user"
    model = Model(ModelResponse(content="Recorded direction A."))
    second = CoordinatorAgent(tmp_path, model_client=model).go("/answer Direction A")
    assert first.session_id == second.session_id
    assert RoleSession(tmp_path, 'main').load()['project_goal'] == 'Help decide'
    assert "Which research direction?" in json.dumps(model.requests[0])
    decision = json.loads((tmp_path / ".paper-agent/main-decision.json").read_text())
    assert decision["status"] == "answered" and decision["answer"] == "Direction A"


@pytest.mark.parametrize("path", ["../outside.md", ".env", ".paper-agent/role-sessions/main.json", "/etc/passwd"])
def test_main_file_reader_rejects_private_or_external_paths(tmp_path, path):
    agent = CoordinatorAgent(tmp_path, model_client=Model())
    with pytest.raises(ValueError):
        agent._read(None, path)


def test_main_file_reader_returns_cache_for_duplicate_range(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\nverified evidence\n", encoding="utf-8")
    agent = CoordinatorAgent(tmp_path, model_client=Model())
    first = json.loads(agent._read(None, "report.md").output)
    second = json.loads(agent._read(None, "report.md").output)
    assert first["sha256"] and "content" in first
    assert second["cached"] is True and "content" not in second
    agent._working_context_compacted("coordinator", 3)
    assert "content" in json.loads(agent._read(None, "report.md").output)
    report.write_text("# Updated evidence\n", encoding="utf-8")
    updated = json.loads(agent._read(None, "report.md").output)
    assert updated["sha256"] != first["sha256"] and "Updated" in updated["content"]


def test_artifact_handoff_contains_hash_and_compact_preview(tmp_path):
    report = tmp_path / "literature" / "RESEARCH_REPORT.md"
    report.parent.mkdir()
    report.write_text("# Evidence\nA verified result.\n", encoding="utf-8")
    agent = CoordinatorAgent(tmp_path, model_client=Model())
    handoff = agent._artifact_handoff(["literature/RESEARCH_REPORT.md"])
    assert handoff[0]["sha256"] and "# Evidence" in handoff[0]["preview"]


def test_refine_assignment_forces_expert_approval(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path, "awaiting_user")
    (tmp_path / "idea-stage").mkdir()
    (tmp_path / "idea-stage/IDEA_REPORT.md").write_text("Idea")
    result = CoordinatorAgent(tmp_path, model_client=Model(
        call("delegate_stage", assignment=assignment("refine")),
        ModelResponse(content="Approve this proposal.")), mode="auto").go("Develop a proposal")
    assert calls[0][1]["mode"] == "expert"
    assert result.status == "awaiting_user"


def test_stage_role_reaches_real_phase_session(tmp_path, monkeypatch):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent import workflow_cli

    class WorkerModel:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                return ModelResponse(content="Literature saved")
            return call("write", path="literature/RESEARCH_REPORT.md", content="Verified report")

    def make_agent(project, **kwargs):
        kwargs.pop("model", None)
        return PhaseAgent(project, model_client=WorkerModel(), **kwargs)

    monkeypatch.setattr(workflow_cli, "PhaseAgent", make_agent)
    task = assignment("literature", "paper_writer")
    model = Model(call("delegate_stage", assignment=task),
                  ModelResponse(content="Checked the literature report."))
    result = CoordinatorAgent(tmp_path, model_client=model, stage_task=task).go("Run only literature")
    assert result.status == "completed", result.error
    writer = RoleSession(tmp_path, "paper_writer").load()
    assert writer["tasks"][-1]["phase"] == "literature"
    assert not RoleSession(tmp_path, "idea_generator").path.exists()


def test_unrecorded_main_tool_result_prevents_replay(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    session = RoleSession(tmp_path, "main")
    with session.locked():
        data = session.load()
        data["messages"] = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "unknown", "type": "function", "function": {
                "name": "delegate_stage", "arguments": json.dumps({"assignment": assignment("literature")})}}]}]
        session.save(data)
    result = CoordinatorAgent(tmp_path, model_client=Model(
        call("delegate_stage", assignment=assignment("literature")),
        ModelResponse(content="Inspect previous work before retry."))).go("Continue")
    assert result.status == "interrupted" and calls == []


def test_assignment_budget_does_not_execute_additional_work(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    model = Model(call("delegate_stage", assignment=assignment("literature")),
                  call("delegate_stage", assignment=assignment("data_search")),
                  ModelResponse(content="Budget reached; continue next time."))
    result = CoordinatorAgent(tmp_path, model_client=model, max_assignments=1).go("Research")
    assert result.status == "awaiting_user" and len(calls) == 1


def test_stage_prerequisites_are_checked_before_execution(tmp_path, monkeypatch):
    calls = stub_stages(monkeypatch, tmp_path)
    result = CoordinatorAgent(tmp_path, model_client=Model(
        call("delegate_stage", assignment=assignment("ideas")),
        ModelResponse(content="Literature and data reports are missing."))).go("Generate ideas")
    assert result.status == "failed" and calls == []


def test_explicit_revision_retains_scope_and_returns_result(tmp_path, monkeypatch):
    from paper_agent.agent import node_task
    calls = []
    task = {"node": "paper_figure", "target": "figure 1", "instruction": "Change layout",
            "files": ["figures/figure_1.pdf"], "compile_pdf": False}
    def revise(project, submitted, prompt, folder, **kwargs):
        calls.append(submitted)
        return SimpleNamespace(status="completed", error=None)
    monkeypatch.setattr(node_task, "run_node_task", revise)
    result = CoordinatorAgent(tmp_path, model_client=Model(
        call("revise_artifact", task=task), ModelResponse(content="Figure 1 revised.")),
        revision_task=task).go("Change figure 1 only")
    assert result.status == "completed" and calls == [task]


def test_dispatch_and_worker_share_main_memory(tmp_path, monkeypatch):
    from paper_agent.api import dispatch
    initial = Model(ModelResponse(content="The user prefers method A."))
    monkeypatch.setattr(dispatch, "ModelClient", lambda **kwargs: initial)
    dispatch.dispatch_message({"path": str(tmp_path), "name": "test"}, "Remember method A")
    model = Model(ModelResponse(content="Continuing with method A."))
    result = CoordinatorAgent(tmp_path, model_client=model).go("Continue")
    assert result.status == "completed"
    assert "The user prefers method A." in json.dumps(model.requests[0])


def test_busy_main_only_returns_status_without_calling_model(tmp_path, monkeypatch):
    from paper_agent.api import dispatch
    monkeypatch.setattr(dispatch, "ModelClient", lambda **kwargs: pytest.fail("Do not call a concurrent model"))
    session = RoleSession(tmp_path, "main")
    with session.locked():
        result = dispatch.dispatch_message({"path": str(tmp_path), "name": "test"}, "Progress?")
    assert result["tool"] == "reply" and "主控正在处理" in result["reply"]
