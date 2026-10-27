import asyncio
import hashlib

import pytest

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState
from paper_agent.agent.workflow_cli import WORKFLOW_NODE_NAMES
from paper_agent.tools import ToolContext, invoke
from test_workflow import ResearchRecordingAgent


def test_literature_data_search_ideas_handoff(tmp_path, monkeypatch):
    class Agent(ResearchRecordingAgent):
        def go(self, prompt, *, phase=None, completion_check=None):
            if phase in {"literature", "data_search"}:
                self.calls.append((phase, prompt))
                if phase == "data_search":
                    assert "Prior battery literature finding" not in prompt
                    (self.project / "data-search/DATA_REPORT.md").write_text("Verified complete package")
                if completion_check:
                    completion_check()
                return type("Result", (), {"iterations": 1})()
            return super().go(prompt, phase=phase, completion_check=completion_check)

    agent = Agent(tmp_path, None)
    runner = WorkflowRunner(agent)
    monkeypatch.setattr(runner, "_research_context", lambda *args, **kwargs: {})
    state = runner.run("Study battery degradation", stop_after="ideas")
    assert state.status == "completed", state.error
    assert [phase for phase, _ in agent.calls] == ["literature", "data_search", "ideas"]
    assert "data-search/DATA_REPORT.md" in agent.calls[-1][1]
    assert state.artifacts["data_search"] == "data-search/DATA_REPORT.md"
    assert state.node_results["ideas"]["data_source"] == "data-search/DATA_REPORT.md"
    assert state.node_results["ideas"]["data_sha256"] == hashlib.sha256(b"Verified complete package").hexdigest()
    assert "data_search" in WORKFLOW_NODE_NAMES


@pytest.mark.parametrize("report", [None, "  "])
def test_ideas_require_data_report_before_model_call(tmp_path, report):
    agent = ResearchRecordingAgent(tmp_path, None)
    path = tmp_path / "data-search/DATA_REPORT.md"
    if report is None:
        path.unlink()
    else:
        path.write_text(report)
    runner = WorkflowRunner(agent)
    with pytest.raises((RuntimeError, ValueError), match="DATA_REPORT.md"):
        runner._run_ideas(WorkflowState("test"), "Battery research")
    assert not agent.calls
    if report is None:
        with pytest.raises(RuntimeError, match="DATA_REPORT.md"):
            runner.start_from_project("ideas", "Battery research")


def test_data_search_explores_package_and_enforces_report_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "literature").mkdir()
    (project / "literature/RESEARCH_REPORT.md").write_text("Study degradation mechanisms")
    package = tmp_path / "package"
    (package / "Tongji").mkdir(parents=True)
    (package / "Life labels").mkdir()
    label = package / "Life labels/Tongji_labels.json"
    label.write_text('{"cell": 120}')
    knowledge = tmp_path / "knowledge"
    (knowledge / "数据集").mkdir(parents=True)
    (knowledge / "数据集/Package.md").write_text(f'- 数据集根路径：`{package}`\n')

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            names = {item["function"]["name"] for item in tools}
            assert {"ls", "bash", "read", "inspect_battery_data"} <= names
            if self.calls == 1:
                assert "Study degradation mechanisms" not in messages[1]["content"]
                assert str(package) in messages[1]["content"]
                return ModelResponse(tool_calls=(ToolCall("list", "ls", {"path": str(package)}),))
            if self.calls == 2:
                assert "Life labels/" in messages[-1]["content"]
                return ModelResponse(tool_calls=(ToolCall("label", "read", {"path": str(label)}),))
            if self.calls == 3:
                assert '"cell": 120' in messages[-1]["content"]
                return ModelResponse(content="Finished without report")
            if self.calls == 4:
                assert "DATA_REPORT.md" in messages[-1]["content"]
                return ModelResponse(tool_calls=(ToolCall("save", "write", {
                    "path": "data-search/DATA_REPORT.md", "content": "Observed label cell=120"}),))
            return ModelResponse(content="Saved")

    agent = PhaseAgent(project, model_client=Model(), knowledge_root=knowledge, phase="data_search", max_iterations=5)
    runner = WorkflowRunner(agent)
    state = runner.start_from_project("data_search", "Understand dataset")
    state = runner.run("Understand dataset", state=state, stop_after="data_search")
    assert state.status == "completed", state.error
    assert (project / "data-search/DATA_REPORT.md").read_text() == "Observed label cell=120"
    assert label.read_text() == '{"cell": 120}'
    ctx = ToolContext(project, knowledge_roots=(knowledge,))
    assert asyncio.run(invoke("write", ctx, path=str(label), content="bad")).is_error
    assert asyncio.run(invoke("read", ctx, path=str(tmp_path / "outside.json"))).is_error


def test_data_report_is_not_embedded_in_downstream_planning(tmp_path, monkeypatch):
    agent = ResearchRecordingAgent(tmp_path, None)
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="refine"))
    monkeypatch.setattr(runner, "_research_context", lambda *args, **kwargs: {})
    (tmp_path / "idea-stage").mkdir()
    (tmp_path / "idea-stage/IDEA_REPORT.md").write_text("Idea")
    state = runner.start_from_project("refine", "Battery research")
    state = runner.run("Battery research", state=state, stop_after="refine")
    assert state.status == "completed", state.error
    assert "Prior verified dataset finding" not in agent.calls[-1][1]


def test_data_search_rejects_missing_literature(tmp_path):
    agent = ResearchRecordingAgent(tmp_path, None)
    (tmp_path / "literature/RESEARCH_REPORT.md").unlink()
    runner = WorkflowRunner(agent)
    with pytest.raises(RuntimeError, match="RESEARCH_REPORT.md"):
        runner.start_from_project("data_search", "Research")
