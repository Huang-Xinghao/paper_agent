import json
from types import SimpleNamespace

import pytest

from paper_agent.agent.workflow import NodeResult, WorkflowConfig, WorkflowRunner, WorkflowState
from paper_agent.agent.workflow_cli import WORKFLOW_NODE_NAMES
from paper_agent.tools.profiles import PHASE_TOOL_NAMES
from test_workflow import CompilingRunner, StubAgent


def test_main_agent_can_stop_at_pdf_before_improvement(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_AGENT_ENABLE_IMAGE_FIGURE", "false")
    agent = StubAgent([], tmp_path)
    runner = CompilingRunner(agent, config=WorkflowConfig(start_node="ideas"))
    state = runner.run("Research", stop_after="paper_compile")
    assert state.status == "awaiting_results"
    (tmp_path / "data.csv").write_text("real fixture input")
    state = runner.run("Research", state=WorkflowState.from_dict(state.to_dict()), stop_after="paper_compile")
    assert state.status == "completed"
    assert state.current_node == "paper_compile"
    assert state.paper_status == "pdf_ready"
    assert agent.phases == ["ideas", "research_refine", "experiment_plan",
                            "experiment_bridge", "experiment_bridge", "paper_plan", "paper_figure", "paper_write"]
    assert "review" not in state.to_dict()
    assert (tmp_path / "paper/main.pdf").is_file()
    assert not (tmp_path / "idea-stage/REVIEW.json").exists()
    assert not (tmp_path / ".paper-agent/experiment-code-reviews").exists()
    assert not (tmp_path / "paper/PAPER_IMPROVEMENT_LOG.md").exists()
    assert "external_review" not in PHASE_TOOL_NAMES["ideas"]
    assert "novelty" not in runner._nodes
    assert "novelty" not in WORKFLOW_NODE_NAMES
    assert not (tmp_path / "idea-stage/NOVELTY.json").exists()


def test_refine_entry_requires_only_idea(tmp_path):
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    with pytest.raises(RuntimeError, match="IDEA_REPORT.md"):
        runner.start_from_project("refine", "Research")
    root = tmp_path / "idea-stage"
    root.mkdir()
    (root / "IDEA_REPORT.md").write_text("Complete idea")
    state = runner.start_from_project("refine", "Research")
    runner.run("Research", state=state, stop_after="refine")
    assert state.status == "completed"
    assert (tmp_path / "refine-logs/FINAL_PROPOSAL.md").is_file()


@pytest.mark.parametrize("old_node", [
    "novelty", "experiment_check", "experiment_run", "review",
    "experiment_code_review", "auto_paper_improvement_loop",
])
def test_removed_nodes_are_rejected(tmp_path, old_node):
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path), config=WorkflowConfig(start_node="ideas"))
    assert old_node not in runner._nodes
    assert old_node not in WORKFLOW_NODE_NAMES
    with pytest.raises(ValueError, match="unknown workflow node"):
        runner.start_from_project(old_node, "Research")
    state = WorkflowState("old", current_node=old_node)
    runner.run("Research", state=state)
    assert state.status == "failed"
    assert "unknown workflow node" in state.error
    assert state.history == []
    assert state.current_node == old_node
