import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent import workflow_cli, loop
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.workflow import NodeResult, WorkflowState


@pytest.mark.parametrize("failed_round", [1, 2])
def test_resume_review_timeout_preserves_completed_round(tmp_path, monkeypatch, failed_round):
    from paper_agent.agent.workflow import WorkflowRunner
    from paper_agent.agent.review_runner import ReviewRunner

    paper = tmp_path / "paper"
    paper.mkdir()
    for name in ("main.tex", "main.pdf", "main_round0_original.pdf", "main_round1.pdf",
                 "review_round1.md", "PAPER_IMPROVEMENT_LOG.md"):
        (paper / name).write_text(name)
    saved = WorkflowState("old", task_prompt="no new experiments", status="failed",
                          failed_node="improvement", current_node="improvement", mode="expert",
                          error=f"node improvement failed: Paper review round {failed_round} did not complete: Review exceeded 1800 seconds")
    (tmp_path / "workflow-state.json").write_text(json.dumps(saved.to_dict()))
    seen = []

    def review(self, request):
        seen.append(request.output.name)
        assert self.timeout == 5400
        return SimpleNamespace(status="timed_out", error="test timeout", elapsed_seconds=5400)

    monkeypatch.setattr(ReviewRunner, "run", review)
    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda *a, **k: SimpleNamespace(project=tmp_path))
    state = workflow_cli.run_workflow(tmp_path, saved.task_prompt, start_node="improvement",
                                      paper_review_timeout=5400, paper_review_round2_timeout=5400)
    assert state.status == "failed"
    assert seen == [f"review_round{failed_round}.md"]
    assert "test timeout" in state.error
    if failed_round == 2:
        for name in ("main_round0_original.pdf", "main_round1.pdf", "review_round1.md",
                     "PAPER_IMPROVEMENT_LOG.md"):
            assert (paper / name).read_text() == name


@pytest.mark.parametrize('options', [
    ['--start-node', 'paper_figure', '--stop-after', 'paper_figure'],
    ['--single-node', 'paper_figure'],
])
def test_cli_single_figure_entry_and_stop(tmp_path, monkeypatch, options):
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return WorkflowState('test', status='completed')
    monkeypatch.setattr(workflow_cli, 'run_workflow', run)
    monkeypatch.setattr(sys, 'argv', ['paper-workflow', '--project', str(tmp_path), *options, 'Only Figure 1'])
    workflow_cli.main()
    assert captured['start_node'] == captured['stop_after'] == 'paper_figure'


def test_cli_rejects_conflicting_single_node_options(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['paper-workflow', '--project', str(tmp_path),
                                    '--single-node', 'paper_figure', '--stop-after', 'paper_write', 'task'])
    with pytest.raises(SystemExit) as exc:
        workflow_cli.main()
    assert exc.value.code == 2


def test_agent_reports_model_and_tool_progress(tmp_path):
    events = []

    class Model:
        def complete(self, messages, tools):
            if not any(message["role"] == "tool" for message in messages):
                return ModelResponse(tool_calls=(ToolCall("1", "write", {
                    "path": "idea-stage/IDEA_REPORT.md", "content": "Idea"}),))
            return ModelResponse(content="done")

    agent = PhaseAgent(tmp_path, model_client=Model(), on_progress=lambda kind, data: events.append((kind, data)))
    agent.go("create an idea")

    assert [kind for kind, _ in events] == [
        "model_start", "model_end", "tool_start", "tool_end",
        "model_start", "model_end", "assistant_output", "phase_end",
    ]
    assert events[2][1]["tool"] == "write"
    assert json.loads(events[2][1]["arguments"])["path"] == "idea-stage/IDEA_REPORT.md"
    assert events[3][1]["call_id"] == "1"
    assert events[3][1]["output"]
    assert events[6][1]["content"] == "done"


def test_workflow_cli_persists_failed_state_and_progress(tmp_path, monkeypatch, capsys):
    class Runner:
        def __init__(self, agent, *, on_node_start, on_node_end, on_node_progress):
            self.agent = agent
            self.on_node_start = on_node_start
            self.on_node_end = on_node_end

        def run(self, prompt):
            state = WorkflowState("test-run", task_prompt=prompt)
            self.on_node_start("ideas", state)
            self.agent.on_progress("model_start", {"phase": "ideas", "iteration": 1})
            self.agent.on_progress("model_error", {"phase": "ideas", "iteration": 1,
                                                    "error": "Request timed out."})
            state.status = "failed"
            state.error = "node ideas failed: Request timed out."
            return state

    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kwargs:
                        SimpleNamespace(on_progress=kwargs["on_progress"]))
    monkeypatch.setattr(workflow_cli, "WorkflowRunner", Runner)

    state = workflow_cli.run_workflow(tmp_path, "test task")

    progress = json.loads((tmp_path / "workflow-progress.json").read_text())
    persisted = json.loads((tmp_path / "workflow-state.json").read_text())
    assert state.status == progress["status"] == persisted["status"] == "failed"
    assert progress["current_node"] == "ideas"
    assert progress["run_id"] == "test-run"
    assert [event["kind"] for event in progress["recent_events"]] == [
        "workflow_start", "node_start", "model_start", "model_error", "workflow_end",
    ]
    assert "model_error" in capsys.readouterr().out


def test_workflow_cli_exits_nonzero_on_failed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_cli, "run_workflow", lambda *args, **kwargs: WorkflowState(
        "test-run", status="failed", error="Request timed out."))
    monkeypatch.setattr(sys, "argv", ["paper-workflow", "--project", str(tmp_path), "task"])

    with pytest.raises(SystemExit, match="1"):
        workflow_cli.main()


def test_tool_round_option_reaches_agent_configuration(tmp_path, monkeypatch):
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return WorkflowState('test', status='completed')
    monkeypatch.setattr(workflow_cli, 'run_workflow', run)
    monkeypatch.setattr(sys, 'argv', ['paper-workflow', '--project', str(tmp_path),
                                    '--max-tool-rounds', '40', 'task'])
    workflow_cli.main()
    assert captured['max_iterations'] == 40


def test_workflow_cli_starts_at_requested_node_from_saved_state(tmp_path, monkeypatch):
    saved = WorkflowState("prior-run", task_prompt="Research", status="failed",
                          current_node="ideas", transitions=2)
    (tmp_path / "workflow-state.json").write_text(json.dumps(saved.to_dict()))

    class Runner:
        def __init__(self, agent, *, on_node_start, on_node_end, on_node_progress):
            self.on_node_end = on_node_end

        def start_from_project(self, node, prompt, state):
            assert (node, prompt, state.run_id) == ("refine", "Research", "prior-run")
            state.current_node = node
            state.status = "running"
            return state

        def run(self, prompt, *, state):
            state.current_node = "refine"
            state.transitions += 1
            self.on_node_end("refine", state, NodeResult("checked"))
            assert json.loads((tmp_path / "workflow-state.json").read_text())["current_node"] == "refine"
            state.status = "completed"
            return state

    monkeypatch.setattr(workflow_cli, "PhaseAgent", lambda project, **kwargs: SimpleNamespace())
    monkeypatch.setattr(workflow_cli, "WorkflowRunner", Runner)
    state = workflow_cli.run_workflow(tmp_path, "Research", start_node="refine")
    assert state.run_id == "prior-run"
    assert state.status == "completed"


@pytest.mark.parametrize("status,exit_code", [
    ("awaiting_results", 2),
])
def test_workflow_cli_exits_nonzero_for_unfinished_states(tmp_path, monkeypatch, status, exit_code):
    monkeypatch.setattr(workflow_cli, "run_workflow", lambda *args, **kwargs:
                        WorkflowState("test-run", status=status))
    monkeypatch.setattr(sys, "argv", ["paper-workflow", "--project", str(tmp_path), "task"])
    with pytest.raises(SystemExit) as exc:
        workflow_cli.main()
    assert exc.value.code == exit_code




@pytest.mark.parametrize("entry", [workflow_cli.main, loop.main])
def test_old_tool_round_option_is_rejected(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(sys, "argv", ["paper-agent", "--project", str(tmp_path),
                                    "--max-iterations", "40", "task"])
    with pytest.raises(SystemExit) as error:
        entry()
    assert error.value.code == 2
