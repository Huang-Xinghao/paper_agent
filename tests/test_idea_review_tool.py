import asyncio

import pytest

from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.review_runner import ReviewResult
from paper_agent.agent import review_runner
from paper_agent.tools import BUILTIN_TOOLS, ToolContext, invoke, tools_for_phase


def prepare_inputs(project):
    for name in ("idea-stage/IDEA_CANDIDATES.md", "literature/RESEARCH_REPORT.md",
                 "data-search/DATA_REPORT.md"):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Candidate evidence", encoding="utf-8")


def fake_runner(monkeypatch, project, status="completed"):
    requests = []
    report = project / "idea-stage/review.md"

    class Runner:
        def __init__(self, workspace):
            assert workspace == project

        def run(self, request):
            requests.append(request)
            if status == "completed":
                report.write_text("Prefer candidate B: candidate A has data leakage.")
            return ReviewResult(status, report, "review-thread", 0, 1, None,
                                None if status == "completed" else "Reviewer timed out")

    monkeypatch.setattr(review_runner, "ReviewRunner", Runner)
    return requests, report


def test_candidate_review_returns_to_same_creator_conversation(tmp_path, monkeypatch):
    prepare_inputs(tmp_path)
    requests, report = fake_runner(monkeypatch, tmp_path)

    class Model:
        count = 0

        def complete(self, messages, tools):
            self.count += 1
            if self.count == 1:
                assert "review_idea" in {t["function"]["name"] for t in tools}
                return ModelResponse(content="Candidates A and B are ready.", tool_calls=(
                    ToolCall("review", "review_idea", {"instructions": "Check leakage."}),))
            if self.count == 2:
                assert any(m.get("content") == "Candidates A and B are ready." for m in messages)
                assert messages[-1]["role"] == "tool"
                assert "Prefer candidate B" in messages[-1]["content"]
                assert "data leakage" in (tmp_path / "idea-stage/review.md").read_text()
                return ModelResponse(tool_calls=(ToolCall("final", "write", {
                    "path": "idea-stage/IDEA_REPORT.md", "content": "Choose B, avoiding leakage."}),))
            return ModelResponse(content="Final idea report ready.")

    result = PhaseAgent(tmp_path, model_client=Model()).go("Compare the candidates")
    assert result.final == "Final idea report ready."
    assert (tmp_path / "idea-stage/IDEA_REPORT.md").read_text() == "Choose B, avoiding leakage."
    assert (tmp_path / "idea-stage/review.md").read_text() == report.read_text()
    assert requests[0].kind == "idea"
    assert [str(p) for p in requests[0].inputs] == [
        "idea-stage/IDEA_CANDIDATES.md", "literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md"]
    assert "Check leakage." in requests[0].instructions


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_failed_review_does_not_publish_partial_report(tmp_path, monkeypatch, status):
    prepare_inputs(tmp_path)
    fake_runner(monkeypatch, tmp_path, status)
    target = tmp_path / "idea-stage/review.md"
    target.write_text("Previous review")
    result = asyncio.run(invoke("review_idea", ToolContext(tmp_path)))
    assert result.is_error
    assert result.metadata["status"] == status
    assert "earlier run" in result.output
    assert target.read_text() == "Previous review"
    assert not (tmp_path / "idea-stage/IDEA_REPORT.md").exists()


def test_review_requires_candidate_inputs_before_launch(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Must not launch reviewer without inputs")

    monkeypatch.setattr(review_runner, "ReviewRunner", unexpected)
    result = asyncio.run(invoke("review_idea", ToolContext(tmp_path)))
    assert result.is_error
    assert "IDEA_CANDIDATES.md" in result.output


def test_review_output_and_phase_scope(tmp_path, monkeypatch):
    prepare_inputs(tmp_path)
    requests, report = fake_runner(monkeypatch, tmp_path)
    result = asyncio.run(invoke("review_idea", ToolContext(tmp_path)))
    assert not result.is_error
    assert result.metadata["report_path"] == "idea-stage/review.md"
    assert not (tmp_path / "reviews").exists()
    for phase in ("ideas", "literature", "research_refine", "experiment_bridge"):
        names = {t.name for t in tools_for_phase(phase, BUILTIN_TOOLS)}
        assert ("review_idea" in names) == (phase == "ideas")
