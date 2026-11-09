import copy
import json

import pytest

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.role_sessions import PhaseTask, RoleSession, PHASE_ROLES


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        response = next(self.responses, ModelResponse(content="done"))
        if isinstance(response, BaseException):
            raise response
        return response


def run(agent, phase, instruction, role=None):
    return agent.execute(PhaseTask(phase, instruction, role), completion_check=lambda: None)


def test_role_continues_across_phases_and_process_instances(tmp_path):
    model = Model(ModelResponse(content="literature conclusion"))
    first = run(PhaseAgent(tmp_path, model_client=model), "literature", "Study evidence")
    model = Model()
    second = run(PhaseAgent(tmp_path, model_client=model), "research_refine", "Refine the proposal")
    messages, tools = model.requests[0]
    assert first.session_id == second.session_id
    assert first.task_id != second.task_id
    assert second.role == "idea_generator"
    assert any(m.get("content") == "literature conclusion" for m in messages)
    assert sum(m["role"] == "system" for m in messages) == 1
    assert "Current phase: research_refine" in messages[0]["content"]
    assert "## Skill: research-refine" in messages[0]["content"]
    assert "## Skill: research-lit" not in messages[0]["content"]
    assert "bash" in {t["function"]["name"] for t in tools}
    assert not any(m.get("content") == "literature conclusion" for m in second.task_messages)


def test_roles_and_projects_have_separate_memory(tmp_path):
    model = Model(ModelResponse(content="private idea discussion"))
    idea = run(PhaseAgent(tmp_path, model_client=model), "ideas", "Idea task")
    experiment = run(PhaseAgent(tmp_path, model_client=model), "experiment_plan", "Plan experiments")
    assert idea.session_id != experiment.session_id
    assert "private idea discussion" not in json.dumps(model.requests[-1][0])
    other = run(PhaseAgent(tmp_path / "other", model_client=model), "ideas", "Another project")
    assert other.session_id != idea.session_id
    assert "private idea discussion" not in json.dumps(model.requests[-1][0])


def test_explicit_assignment_can_use_main_or_override_default_role(tmp_path):
    model = Model()
    for role in ("main", "paper_writer"):
        state = run(PhaseAgent(tmp_path, model_client=model), "novelty", "Check this claim", role)
        assert state.role == role
        assert f"Persistent role: {role}" in state.messages[0]["content"]
        names = {t["function"]["name"] for t in model.requests[-1][1]}
        assert "bash" not in names  # The assigned phase controls tools, not the role.


def test_resume_retains_tool_results_without_replaying_calls(tmp_path):
    response = ModelResponse(tool_calls=(ToolCall("save", "write", {"path": "note.md", "content": "saved"}),))
    agent = PhaseAgent(tmp_path, model_client=Model(response, RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        run(agent, "literature", "Write a note")
    stored = RoleSession(tmp_path, "idea_generator").load()
    assert stored["tasks"][-1]["status"] == "failed"
    model = Model()
    state = run(PhaseAgent(tmp_path, model_client=model), "ideas", "Continue with current files")
    assert any(m.get("tool_call_id") == "save" for m in model.requests[0][0])
    assert (tmp_path / "note.md").read_text() == "saved"
    assert not any(m["role"] == "tool" for m in state.task_messages)


def test_interrupted_unrecorded_tool_result_is_closed_as_uncertain(tmp_path):
    session = RoleSession(tmp_path, "experiment")
    with session.locked():
        data = session.load()
        data["messages"] = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "unknown", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]}]
        data["tasks"] = [{"id": "old", "status": "running"}]
        session.save(data)
    model = Model()
    run(PhaseAgent(tmp_path, model_client=model), "experiment_bridge", "Inspect before continuing")
    messages = model.requests[0][0]
    uncertain = next(m for m in messages if m.get("tool_call_id") == "unknown")
    assert uncertain["is_error"] and "may have run" in uncertain["content"]
    assert session.load()["tasks"][0]["status"] == "interrupted"


def test_role_lock_rejects_concurrent_writer_and_duplicate_task_id(tmp_path):
    session = RoleSession(tmp_path, "idea_generator")
    agent = PhaseAgent(tmp_path, model_client=Model())
    with session.locked(), pytest.raises(RuntimeError, match="already running"):
        run(agent, "ideas", "Concurrent task")
    task = PhaseTask("ideas", "Unique assignment", id="one")
    agent.execute(task, completion_check=lambda: None)
    with pytest.raises(ValueError, match="already exists"):
        agent.execute(task, completion_check=lambda: None)


def test_compaction_archives_full_history_and_preserves_role_identity(tmp_path):
    agent = PhaseAgent(tmp_path, model_client=Model(ModelResponse(content="evidence " * 200)),
                       context_max_chars=1000)
    first = run(agent, "literature", "Read papers")
    model = Model(ModelResponse(content="Verified evidence in note.md; unresolved hypothesis H1."),
                  ModelResponse(content="new result"))
    second = run(PhaseAgent(tmp_path, model_client=model, context_max_chars=1000),
                 "ideas", "Develop ideas")
    assert first.session_id == second.session_id
    assert model.requests[0][1] == []
    assert "Historical role memory" in model.requests[1][0][1]["content"]
    assert "note.md" in model.requests[1][0][1]["content"]
    assert list((tmp_path / ".paper-agent/role-sessions/archives/idea_generator").glob("*.json"))


def test_failed_summary_preserves_original_history(tmp_path):
    run(PhaseAgent(tmp_path, model_client=Model(ModelResponse(content="x" * 2000))), "ideas", "First task")
    session = RoleSession(tmp_path, "idea_generator")
    original = session.path.read_bytes()
    with pytest.raises(RuntimeError, match="summarization failed"):
        run(PhaseAgent(tmp_path, model_client=Model(ModelResponse()), context_max_chars=1000),
            "research_refine", "Next task")
    assert session.path.read_bytes() == original


def test_task_events_identify_role_session_and_assignment(tmp_path):
    events = []
    agent = PhaseAgent(tmp_path, model_client=Model(),
                       on_progress=lambda kind, data: events.append((kind, data)))
    state = run(agent, "paper_plan", "Outline")
    for kind, data in events:
        assert data["role"] == "paper_writer"
        assert data["session_id"] == state.session_id
        assert data["task_id"] == state.task_id
    assert events[-1][1]["status"] == "completed"


def test_workflow_routes_consecutive_stages_into_one_role_session(tmp_path, monkeypatch):
    from paper_agent.agent.workflow import WorkflowRunner

    responses = []
    for path in ("literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md", "idea-stage/IDEA_REPORT.md"):
        responses.extend([
            ModelResponse(tool_calls=(ToolCall(path, "write", {"path": path, "content": "Evidence-backed report"}),)),
            ModelResponse(content=f"Saved {path}"),
        ])
    agent = PhaseAgent(tmp_path, model_client=Model(*responses))
    runner = WorkflowRunner(agent)
    monkeypatch.setattr(runner, "_research_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner, "_wiki_context", lambda *args, **kwargs: {"status": "unavailable"})
    state = runner.run("Investigate a research question", stop_after="ideas")
    assert state.status == "completed", state.error
    results = [state.node_results[node] for node in ("literature", "data_search", "ideas")]
    assert {result["role"] for result in results} == {"idea_generator"}
    assert len({result["session_id"] for result in results}) == 1
    assert len({result["task_id"] for result in results}) == 3
    assert all(result["tool_calls"] == ["write"] for result in results)


@pytest.mark.parametrize("reject", [False, True])
def test_scoped_revision_only_publishes_accepted_role_memory(tmp_path, monkeypatch, reject):
    from paper_agent.agent import node_task

    project = tmp_path / "project"
    project.mkdir()
    (project / "PAPER_PLAN.md").write_text("Original plan")
    run(PhaseAgent(project, model_client=Model(ModelResponse(content="Previous writing decision"))),
        "paper_plan", "Initial task")
    session = RoleSession(project, "paper_writer")
    before = session.path.read_bytes()
    calls = [ToolCall("edit", "write", {"path": "PAPER_PLAN.md", "content": "Updated plan"})]
    if reject:
        calls.append(ToolCall("unscoped", "write", {"path": "unrelated.md", "content": "unwanted"}))
    model = Model(ModelResponse(tool_calls=tuple(calls)), ModelResponse(content="Revised plan"))

    def make_agent(workspace, **kwargs):
        kwargs.pop("model", None)
        return PhaseAgent(workspace, model_client=model, **kwargs)

    monkeypatch.setattr(node_task, "PhaseAgent", make_agent)
    result = node_task.run_node_task(project, {
        "node": "paper_plan", "target": "plan", "instruction": "Update this plan",
        "files": ["PAPER_PLAN.md"],
    }, "Update this plan", tmp_path / "run")
    assert "Previous writing decision" in json.dumps(model.requests[0][0])
    if reject:
        assert result.status == "failed"
        assert session.path.read_bytes() == before
        assert (project / "PAPER_PLAN.md").read_text() == "Original plan"
    else:
        assert result.status == "completed", result.error
        assert session.load()["tasks"][-1]["final"] == "Revised plan"
        assert len(session.load()["tasks"]) == 2
        assert (project / "PAPER_PLAN.md").read_text() == "Updated plan"


@pytest.mark.parametrize("phase,role", list(PHASE_ROLES.items()))
def test_default_assignments(phase, role):
    assert PhaseTask(phase, "task").role == role
