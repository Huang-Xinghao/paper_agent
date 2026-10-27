import asyncio

import pytest

from paper_agent.agent import review_runner
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.review_runner import ReviewResult
from paper_agent.tools import BUILTIN_TOOLS, ToolContext, invoke, tools_for_phase


def prepare(project):
    for name, content in {
        "refine-logs/FINAL_PROPOSAL.md": "Fit preprocessing on train only.",
        "refine-logs/EXPERIMENT_PLAN.md": "Evaluate held-out entities.",
        "experiments/manifest.json": '{"backend": "local"}',
        "experiments/train.py": "scaler.fit(all_data)",
        "data-search/DATA_REPORT.md": "Entity IDs and labels are available.",
    }.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def reviewer(monkeypatch, project, status="completed"):
    requests = []

    class Runner:
        def __init__(self, workspace):
            assert workspace == project

        def run(self, request):
            requests.append(request)
            assert request.kind == "code"
            report = project / request.output
            if status == "completed":
                report.write_text("NEEDS_REVISION: scaler.fit(all_data) leaks test data.")
            return ReviewResult(status, report, "thread", 0, 1, None,
                                "Review unavailable" if status != "completed" else None)

    monkeypatch.setattr(review_runner, "ReviewRunner", Runner)
    return requests


def test_bridge_receives_review_and_owns_repair(tmp_path, monkeypatch):
    prepare(tmp_path)
    requests = reviewer(monkeypatch, tmp_path)

    class Model:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                assert "review_code" in {t["function"]["name"] for t in tools}
                return ModelResponse(content="Implementation ready for review.", tool_calls=(
                    ToolCall("review", "review_code", {
                        "files": ["experiments/train.py", "experiments/train.py"],
                        "instructions": "Check scaler fitting."}),))
            if self.calls == 2:
                assert any(m.get("content") == "Implementation ready for review." for m in messages)
                assert messages[-1]["role"] == "tool"
                assert "leaks test data" in messages[-1]["content"]
                assert (tmp_path / "experiments/train.py").read_text() == "scaler.fit(all_data)"
                return ModelResponse(tool_calls=(ToolCall("repair", "edit", {
                    "path": "experiments/train.py", "old": "all_data", "new": "train_data"}),))
            return ModelResponse(content="Repair ready for experiment execution.")

    agent = PhaseAgent(tmp_path, model_client=Model(), phase="experiment_bridge")
    agent.go("Review and repair", completion_check=lambda: None)
    assert (tmp_path / "experiments/train.py").read_text().splitlines() == ["scaler.fit(train_data)"]
    assert (tmp_path / "refine-logs/CODE_REVIEW.md").is_file()
    assert not (tmp_path / "reviews").exists()
    assert not (tmp_path / "experiments/review.md").exists()
    assert len(requests[0].inputs) == 5
    assert "Check scaler fitting." in requests[0].instructions


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_failed_code_review_preserves_old_report(tmp_path, monkeypatch, status):
    prepare(tmp_path)
    reviewer(monkeypatch, tmp_path, status)
    report = tmp_path / "refine-logs/CODE_REVIEW.md"
    report.write_text("Earlier review")
    result = asyncio.run(invoke("review_code", ToolContext(tmp_path), files=["experiments/train.py"]))
    assert result.is_error
    assert "earlier run" in result.output
    assert report.read_text() == "Earlier review"


@pytest.mark.parametrize("files", [[], ["experiments"], ["missing.py"], ["../outside.py"],
                                    ["refine-logs/CODE_REVIEW.md"]])
def test_invalid_code_scope_never_launches_reviewer(tmp_path, monkeypatch, files):
    prepare(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid scope must not launch reviewer")

    monkeypatch.setattr(review_runner, "ReviewRunner", unexpected)
    result = asyncio.run(invoke("review_code", ToolContext(tmp_path), files=files))
    assert result.is_error


def test_required_context_and_optional_data_report(tmp_path, monkeypatch):
    prepare(tmp_path)
    requests = reviewer(monkeypatch, tmp_path)
    (tmp_path / "data-search/DATA_REPORT.md").unlink()
    result = asyncio.run(invoke("review_code", ToolContext(tmp_path), files=["experiments/train.py"]))
    assert not result.is_error
    assert len(requests[0].inputs) == 4
    (tmp_path / "refine-logs/EXPERIMENT_PLAN.md").unlink()
    result = asyncio.run(invoke("review_code", ToolContext(tmp_path), files=["experiments/train.py"]))
    assert result.is_error
    assert len(requests) == 1


def test_code_review_is_bridge_only():
    for phase in ("experiment_bridge", "ideas", "experiment_plan", "research_refine", "paper_write"):
        names = {t.name for t in tools_for_phase(phase, BUILTIN_TOOLS)}
        assert ("review_code" in names) == (phase == "experiment_bridge")
