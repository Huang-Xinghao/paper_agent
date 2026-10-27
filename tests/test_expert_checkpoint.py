import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent import workflow_cli
from paper_agent.agent.loop import ArtifactValidationError
from paper_agent.agent.workflow import NodeResult, WorkflowConfig, WorkflowRunner, WorkflowState


class LiteratureAgent:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.valid_checkpoint = True

    def go(self, prompt, *, phase, completion_check):
        self.calls.append(prompt)
        root = self.project / "literature"
        root.mkdir(exist_ok=True)
        (root / "RESEARCH_REPORT.md").write_text("Literature evidence", encoding="utf-8")
        if self.valid_checkpoint:
            (root / "EXPERT_CHECKPOINT.json").write_text(json.dumps({
                "summary": "证据支持方向 A，方向 B 数据不足",
                "questions": ["是否聚焦方向 A？", "是否补充方向 B 的数据证据？"],
            }), encoding="utf-8")
        completion_check()
        return SimpleNamespace(iterations=1)


def runner_for(project, mode="expert"):
    agent = LiteratureAgent(project)
    runner = WorkflowRunner(agent, config=WorkflowConfig(mode=mode))
    runner._research_context = lambda *a, **kw: {}
    return runner


def test_expert_pauses_durably_before_data_search(tmp_path):
    runner = runner_for(tmp_path)
    state = runner.run("研究电池")
    state = WorkflowState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert state.status == "awaiting_user"
    assert state.current_node == "literature"
    assert state.mode == "expert"
    assert state.transitions == 1
    assert len(state.expert_checkpoint["questions"]) == 2
    assert state.expert_checkpoint["next_node"] == "data_search"
    assert "专家模式" in runner.agent.calls[0]
    assert runner.run("研究电池", state=state).status == "awaiting_user"
    assert len(runner.agent.calls) == 1


def test_auto_has_no_checkpoint(tmp_path):
    runner = runner_for(tmp_path, mode="auto")
    runner.agent.valid_checkpoint = False
    state = runner.run("Research", stop_after="literature")
    assert state.status == "completed"
    assert state.expert_checkpoint is None
    assert "专家模式" not in runner.agent.calls[0]


def test_approval_reads_edited_file_without_rerunning_literature(tmp_path):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    report = tmp_path / "literature/RESEARCH_REPORT.md"
    report.write_text("User narrowed scope to A", encoding="utf-8")
    # New runner defaults to auto; the saved state's expert mode is authoritative.
    resumed = runner_for(tmp_path, mode="auto")
    prompts = []

    def data_search(state, prompt):
        self = resumed
        prompts.append(prompt)
        assert report.read_text() == state.artifacts["literature"] == "User narrowed scope to A"
        checkpoint = self._project / self._checkpoint_path("data_search")
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(json.dumps({"summary": "Data ready", "questions": ["范围？", "输入？"]}))
        (self._project / "data-search/DATA_REPORT.md").write_text("Data report")
        return NodeResult("data")

    resumed._nodes["data_search"] = data_search
    state = resumed.resume_checkpoint(state)
    state = resumed.run(state.task_prompt, state=state, stop_after="data_search")
    assert state.status == "awaiting_user"
    assert prompts == ["Research"]
    assert not resumed.agent.calls
    assert state.history[-2]["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert state.history[-2]["event"] == "expert_approved"


def test_revision_preserves_goal_and_pauses_again(tmp_path):
    runner = runner_for(tmp_path)
    state = runner.run("Original goal")
    state = runner.resume_checkpoint(state, revise=True)
    state = runner.run("Focus on A", state=state)
    assert state.status == "awaiting_user"
    assert state.task_prompt == "Original goal"
    assert "Focus on A" in runner.agent.calls[-1]
    assert state.node_results["literature"]["node_instructions"] == "Focus on A"
    assert state.expert_checkpoint["status"] == "pending"


def test_stale_checkpoint_cannot_satisfy_revision(tmp_path):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    runner.agent.valid_checkpoint = False
    state = runner.resume_checkpoint(state, revise=True)
    state = runner.run("Revise", state=state)
    assert state.status == "failed"
    assert "EXPERT_CHECKPOINT.json" in state.error


@pytest.mark.parametrize("stop_here", [False, True])
def test_web_revision_advances_once_and_next_node_still_requires_review(tmp_path, stop_here):
    runner = runner_for(tmp_path)
    state = runner.run("Original goal", stop_after="literature" if stop_here else None)
    state = runner.resume_checkpoint(state, revise=True, advance_after_revision=True)
    # Round-trip the in-flight marker just as the worker persists it.
    state = WorkflowState.from_dict(json.loads(json.dumps(state.to_dict())))
    runner.agent.valid_checkpoint = False
    def data_search(state, prompt):
        assert prompt == "Original goal"
        assert state.expert_checkpoint["status"] == "approved"
        assert state.expert_checkpoint["confirmed_files"]
        assert state.artifacts["literature"] == "Literature evidence"
        path = tmp_path / runner._checkpoint_path("data_search")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": "Data ready", "questions": ["范围？", "输入？"]}))
        (tmp_path / "data-search/DATA_REPORT.md").write_text("Data report")
        return NodeResult("data")
    runner._nodes["data_search"] = data_search
    state = runner.run("Focus on A", state=state, stop_after="literature" if stop_here else None)
    assert state.task_prompt == "Original goal"
    assert len(runner.agent.calls) == 2
    assert "专家模式" not in runner.agent.calls[-1]
    assert state.node_results["literature"]["node_instructions"] == "Focus on A"
    assert any(entry.get("event") == "expert_approved" and entry.get("node") == "literature" for entry in state.history)
    if stop_here:
        assert state.status == "completed"
        assert state.current_node == "literature"
    else:
        assert state.status == "awaiting_user"
        assert state.current_node == "data_search"
        assert state.expert_checkpoint["status"] == "pending"


def test_web_revision_cannot_advance_with_invalid_artifacts(tmp_path):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    state = runner.resume_checkpoint(state, revise=True, advance_after_revision=True)
    runner._nodes["literature"] = lambda state, prompt: NodeResult("invalid")
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text("")
    state = runner.run("Revise", state=state)
    assert state.status == "failed"
    assert state.current_node == "literature"
    assert state.failed_node == "literature"
    assert "revision validation failed" in state.error


@pytest.mark.parametrize("questions", [[], ["only one"], ["", "question"], "text"])
def test_checkpoint_questions_are_validated(tmp_path, questions):
    runner = runner_for(tmp_path)
    root = tmp_path / "literature"
    root.mkdir()
    (root / "EXPERT_CHECKPOINT.json").write_text(json.dumps({"summary": "result", "questions": questions}))
    with pytest.raises(ArtifactValidationError, match="questions"):
        runner._read_checkpoint("literature")


def test_cannot_approve_empty_report_or_nonpaused_state(tmp_path):
    runner = runner_for(tmp_path)
    with pytest.raises(ValueError, match="检查点"):
        runner.resume_checkpoint(WorkflowState("test"))
    state = runner.run("Research")
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text(" \n")
    with pytest.raises(ValueError, match="不能为空"):
        runner.resume_checkpoint(state)
    assert state.status == "awaiting_user"


def test_cli_resume_without_prompt_returns_to_same_review(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    state = runner.run("Research", stop_after="literature")
    (tmp_path / "workflow-state.json").write_text(json.dumps(state.to_dict()))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: LiteratureAgent(project))
    monkeypatch.setattr(sys, "argv", ["workflow", "--project", str(tmp_path)])
    workflow_cli.main()
    saved = json.loads((tmp_path / "workflow-state.json").read_text())
    assert saved["task_prompt"] == "Research"
    assert saved["status"] == "completed"
    assert saved["expert_checkpoint"]["status"] == "approved"


@pytest.mark.parametrize("option", ["--continue", "--revise"])
def test_removed_cli_review_commands_are_rejected(tmp_path, monkeypatch, option):
    monkeypatch.setattr(sys, "argv", ["workflow", "--project", str(tmp_path), option])
    with pytest.raises(SystemExit) as exc:
        workflow_cli.main()
    assert exc.value.code == 2


def test_cli_continue_without_checkpoint_fails_before_agent(tmp_path):
    with pytest.raises(ValueError, match="检查点"):
        workflow_cli.run_workflow(tmp_path, continue_run=True)


def test_cli_expert_start_and_continue_to_data_search(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: LiteratureAgent(project))
    monkeypatch.setattr(WorkflowRunner, "_research_context", lambda *a, **kw: {})
    paused = workflow_cli.run_workflow(tmp_path, "Original goal", mode="expert")
    assert paused.status == "awaiting_user"
    progress = json.loads((tmp_path / "workflow-progress.json").read_text())
    assert progress["status"] == "awaiting_user"
    report = tmp_path / "literature/RESEARCH_REPORT.md"
    report.write_text("Confirmed scope")

    def data_search(self, state, prompt):
        assert prompt == "Original goal"
        assert report.read_text() == state.artifacts["literature"] == "Confirmed scope"
        checkpoint = self._project / self._checkpoint_path("data_search")
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(json.dumps({"summary": "Data ready", "questions": ["范围？", "输入？"]}))
        (self._project / "data-search/DATA_REPORT.md").write_text("Data report")
        return NodeResult("data")

    monkeypatch.setattr(WorkflowRunner, "_run_data_search", data_search)
    completed = workflow_cli.run_workflow(tmp_path, continue_run=True, stop_after="data_search")
    assert completed.status == "awaiting_user"
    assert completed.mode == "expert"
    assert completed.current_node == "data_search"
    persisted = json.loads((tmp_path / "workflow-state.json").read_text())
    assert persisted["expert_checkpoint"]["status"] == "pending"
    assert persisted["history"][-2]["event"] == "expert_approved"


def test_interactive_answer_is_saved_and_automatically_continues(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    state = runner.run("Original goal", stop_after="data_search")
    (tmp_path / "workflow-state.json").write_text(json.dumps(state.to_dict()))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["1. 聚焦方向 A；2. 暂不补充 B", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    revision_prompts = []

    class RevisingAgent(LiteratureAgent):
        def go(self, prompt, **kwargs):
            revision_prompts.append(prompt)
            assert "聚焦方向 A" in prompt
            assert "不要仅在末尾追加回答" in prompt
            assert "用户回答" in (self.project / "literature/RESEARCH_REPORT.md").read_text()
            result = super().go(prompt, **kwargs)
            (self.project / "literature/RESEARCH_REPORT.md").write_text(
                "# 已修订报告\n研究范围：聚焦方向 A，不研究 B。\n用户回答：聚焦方向 A；暂不补充 B")
            return result

    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: RevisingAgent(project))
    monkeypatch.setattr(WorkflowRunner, "_research_context", lambda *a, **kw: {})

    def data_search(self, state, prompt):
        report = (tmp_path / "literature/RESEARCH_REPORT.md").read_text()
        assert "聚焦方向 A" in report
        assert "研究范围：聚焦方向 A，不研究 B" in report
        assert state.artifacts["literature"] == report
        assert state.expert_checkpoint["confirmed_sha256"] == hashlib.sha256(
            (tmp_path / "literature/RESEARCH_REPORT.md").read_bytes()).hexdigest()
        assert prompt == "Original goal"
        checkpoint = self._project / self._checkpoint_path("data_search")
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(json.dumps({"summary": "Data ready", "questions": ["范围？", "输入？"]}))
        (self._project / "data-search/DATA_REPORT.md").write_text("Data report")
        return NodeResult("data")

    monkeypatch.setattr(WorkflowRunner, "_run_data_search", data_search)
    result = workflow_cli._interact_at_checkpoint(tmp_path, state, {})
    assert result.status == "completed"
    assert result.current_node == "data_search"
    assert len(revision_prompts) == 1
    assert result.transitions == 3  # Initial literature, revision, data search.


@pytest.mark.parametrize("exit_kind", ["eof", "interrupt", "nonterminal"])
def test_interactive_exit_keeps_checkpoint(tmp_path, monkeypatch, exit_kind):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: exit_kind != "nonterminal")

    def answer(_):
        if exit_kind == "eof":
            raise EOFError
        if exit_kind == "interrupt":
            raise KeyboardInterrupt
        raise AssertionError("Nonterminal must not request input")

    monkeypatch.setattr("builtins.input", answer)
    result = workflow_cli._interact_at_checkpoint(tmp_path, state, {})
    assert result.status == "awaiting_user"
    assert result.expert_checkpoint["status"] == "pending"


def test_directly_edited_report_continues_without_model_revision(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    state = runner.run("Research", stop_after="data_search")
    (tmp_path / "workflow-state.json").write_text(json.dumps(state.to_dict()))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    report = tmp_path / "literature/RESEARCH_REPORT.md"

    def edit_and_submit(_):
        report.write_text("User edited final scope")
        return ""

    monkeypatch.setattr("builtins.input", edit_and_submit)
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kw: LiteratureAgent(project))

    def data_search(self, state, prompt):
        assert state.artifacts["literature"] == report.read_text() == "User edited final scope"
        checkpoint = self._project / self._checkpoint_path("data_search")
        checkpoint.parent.mkdir(exist_ok=True)
        checkpoint.write_text(json.dumps({"summary": "Data ready", "questions": ["范围？", "输入？"]}))
        (self._project / "data-search/DATA_REPORT.md").write_text("Data report")
        return NodeResult("data")

    monkeypatch.setattr(WorkflowRunner, "_run_data_search", data_search)
    result = workflow_cli._interact_at_checkpoint(tmp_path, state, {})
    assert result.status == "completed"
    assert result.transitions == 2


def test_empty_answer_accepts_report_without_revision(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    before = (tmp_path / "literature/RESEARCH_REPORT.md").read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "  ")
    calls = []

    def run(project, **kwargs):
        calls.append(kwargs)
        return WorkflowState("done", status="completed")

    monkeypatch.setattr(workflow_cli, "run_workflow", run)
    result = workflow_cli._interact_at_checkpoint(tmp_path, state, {})
    assert result.status == "completed"
    assert calls == [{"continue_run": True}]
    assert (tmp_path / "literature/RESEARCH_REPORT.md").read_bytes() == before


def test_failed_answer_revision_does_not_enter_next_node(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    state = runner.run("Research")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "只用方向 A")
    calls = []

    def run(project, **kwargs):
        calls.append(kwargs)
        return WorkflowState("failed", status="failed", error="revision failed")

    monkeypatch.setattr(workflow_cli, "run_workflow", run)
    result = workflow_cli._interact_at_checkpoint(tmp_path, state, {})
    assert result.status == "failed"
    assert len(calls) == 1
    assert "revise" in calls[0]
    assert "只用方向 A" in (tmp_path / "literature/RESEARCH_REPORT.md").read_text()


@pytest.mark.parametrize("node", [node for node in workflow_cli.WORKFLOW_NODE_NAMES if node != "refine"])
def test_every_node_pauses_reads_edited_files_and_routes(tmp_path, monkeypatch, node):
    class Agent:
        project = tmp_path

        def go(self, prompt, *, phase, completion_check):
            assert "专家模式" in prompt
            path = tmp_path / WorkflowRunner._checkpoint_path(node)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"summary": node + " 完成", "questions": ["方向？", "取舍？"]}))
            completion_check()
            return SimpleNamespace(iterations=1)

    runner = WorkflowRunner(Agent(), config=WorkflowConfig(mode="expert", start_node=node))
    files = runner._review_files(node)
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Original output")
    runner._nodes[node] = lambda state, prompt: NodeResult(files[0], {"phase": node})
    monkeypatch.setattr(runner, "_experiment_manifest", lambda: {})
    monkeypatch.setattr(runner, "_refresh_experiment_tracker", lambda *a: {})
    monkeypatch.setattr("paper_agent.agent.workflow.write_experiment_tracker", lambda *a: None)
    monkeypatch.setattr("paper_agent.agent.workflow.read_plan_blocks", lambda *a: [])
    compiled = []
    monkeypatch.setattr(runner, "_compile_pdf", lambda: compiled.append(True))
    state = runner.run("Research")
    assert state.status == "awaiting_user"
    assert state.expert_checkpoint["node"] == node
    assert state.expert_checkpoint["files"] == files
    (tmp_path / files[0]).write_text("User updated scope")
    state = runner.resume_checkpoint(state)
    assert state.expert_checkpoint["confirmed_files"][files[0]] == hashlib.sha256(b"User updated scope").hexdigest()
    next_node = runner._next_node(state, node)
    if next_node is None:
        assert state.status == "completed"
    else:
        assert state.status == "running"
        assert state.current_node == next_node
    if node in {"literature", "ideas"}:
        assert state.artifacts[node] == "User updated scope"
    assert bool(compiled) == (node in {"paper_compile", "improvement"})


@pytest.mark.parametrize("node", workflow_cli.WORKFLOW_NODE_NAMES)
def test_stop_after_requires_review_then_finishes_without_rerun(tmp_path, node):
    runner = runner_for(tmp_path)
    files = runner._review_files(node)
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Output")
    checkpoint = {"node": node, "status": "pending", "report": files[0],
                  "next_node": runner._next_node(None, node), "stop_after": node}
    state = WorkflowState("stop", mode="expert", current_node=node, status="awaiting_user",
                          expert_checkpoint=checkpoint, node_results={node: {}})
    runner._experiment_manifest = lambda: {}
    runner._refresh_experiment_tracker = lambda *a: {}
    runner._compile_pdf = lambda: None
    if node == "experiment_plan":
        (tmp_path / files[0]).write_text("### 区块 B1：Main experiment\n- 优先级：必须执行")
    result = runner.resume_checkpoint(state)
    assert result.status == "completed"
    assert result.current_node == node


def test_experiment_review_rejects_changed_or_invalid_jobs(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    for relative in runner._review_files("experiment_bridge"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Output")
    state = WorkflowState("test", mode="expert", current_node="experiment_bridge",
                          status="awaiting_user", expert_checkpoint={
                              "node": "experiment_bridge", "status": "pending",
                              "report": "refine-logs/EXPERIMENT_RESULTS.md", "next_node": "paper_plan"})
    monkeypatch.setattr(runner, "_experiment_manifest", lambda: {})
    monkeypatch.setattr(runner, "_refresh_experiment_tracker", lambda *a: {
        "changed-job": {"status": "pending"}, "missing-output": {"status": "artifact_invalid"}})
    with pytest.raises(ValueError, match="changed-job.*missing-output"):
        runner.resume_checkpoint(state)
    assert state.status == "awaiting_user"
    assert state.expert_checkpoint["status"] == "pending"


def test_answer_revision_in_nonliterature_node_uses_current_files(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    report = tmp_path / "PAPER_PLAN.md"
    report.write_text("Original plan")
    state = WorkflowState("test", status="awaiting_user", current_node="paper_plan",
                          expert_checkpoint={"node": "paper_plan", "report": "PAPER_PLAN.md",
                                             "files": ["PAPER_PLAN.md"], "summary": "Plan ready",
                                             "questions": ["重点？", "顺序？"]})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "突出低温泛化")
    calls = []

    def run(project, **kwargs):
        calls.append(kwargs)
        if "revise" in kwargs:
            assert "paper_plan" in kwargs["revise"]
            assert "PAPER_PLAN.md" in kwargs["revise"]
            assert "literature/RESEARCH_REPORT.md" not in kwargs["revise"]
            return state
        return WorkflowState("done", status="completed")

    monkeypatch.setattr(workflow_cli, "run_workflow", run)
    assert workflow_cli._interact_at_checkpoint(tmp_path, state, {}).status == "completed"
    assert len(calls) == 2
    assert "突出低温泛化" in report.read_text()


@pytest.mark.parametrize("stop_after", [None, "refine"])
def test_refine_skips_review_and_routes_in_expert_mode(tmp_path, stop_after):
    runner = runner_for(tmp_path)
    calls = []

    def refine(state, prompt):
        assert runner._expert_node is None
        calls.append("refine")
        return NodeResult("proposal")

    def plan(state, prompt):
        calls.append("experiment_plan")
        root = tmp_path / "refine-logs"
        root.mkdir(exist_ok=True)
        (root / "EXPERIMENT_PLAN.md").write_text("Plan")
        (root / "experiment_plan_EXPERT_CHECKPOINT.json").write_text(json.dumps({
            "summary": "Plan ready", "questions": ["基线？", "预算？"]}))
        return NodeResult("plan")

    runner._nodes["refine"] = refine
    runner._nodes["experiment_plan"] = plan
    state = WorkflowState("test", mode="expert", current_node="refine", task_prompt="Research")
    result = runner.run("Research", state=state, stop_after=stop_after)
    if stop_after:
        assert result.status == "completed"
        assert calls == ["refine"]
        assert result.expert_checkpoint is None
    else:
        assert result.status == "awaiting_user"
        assert calls == ["refine", "experiment_plan"]
        assert result.expert_checkpoint["node"] == "experiment_plan"
    assert not runner.agent.calls
