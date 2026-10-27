from pathlib import Path
from types import SimpleNamespace

from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner
from paper_agent.agent.workflow_cli import WORKFLOW_NODE_NAMES
from paper_agent.agent.loop import PhaseAgent
from paper_agent.tools import BUILTIN_TOOLS, tools_for_phase


def test_compile_continues_through_full_review_and_acceptance(tmp_path, monkeypatch):
    from paper_agent.agent.review_runner import ReviewRunner

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("original")
    (paper / "main.pdf").write_bytes(b"%PDF-original")
    calls = []
    reviews = []

    def review(self, request):
        reviews.append((request, (paper / "main.tex").read_text()))
        assert Path("paper/main.pdf") in request.inputs
        if len(reviews) == 1:
            assert self.timeout == 1800
            assert not any("review_round" in str(p) or "IMPROVEMENT_LOG" in str(p)
                           for p in request.inputs)
        else:
            assert self.timeout == 1200
            assert Path("paper/review_round1.md") in request.inputs
            assert Path("paper/PAPER_IMPROVEMENT_LOG.md") in request.inputs
            assert "第二轮修改验收" in request.instructions
        output = tmp_path / request.output
        output.write_text(f"Review {len(reviews)}")
        return SimpleNamespace(status="completed", report=output)

    monkeypatch.setattr(ReviewRunner, "run", review)

    class Agent:
        project = tmp_path

        def go(self, prompt, *, phase, completion_check):
            calls.append(phase)
            (paper / "main.tex").write_text(f"revision {len(calls)}")
            with (paper / "PAPER_IMPROVEMENT_LOG.md").open("a") as log:
                log.write(f"Fix {len(calls)}\n")
            return SimpleNamespace(iterations=1)

    runner = WorkflowRunner(Agent(), config=WorkflowConfig(start_node="paper_compile"))

    def compile_pdf():
        (paper / "main.pdf").write_bytes(b"%PDF-" + (paper / "main.tex").read_bytes())
        return paper / "main.pdf"

    monkeypatch.setattr(runner, "_compile_pdf", compile_pdf)
    state = runner.run("task")
    assert state.status == "completed", state.error
    assert state.current_node == "improvement"
    assert list(state.artifacts) == ["paper_compile", "improvement"]
    assert calls == ["improvement", "improvement"]
    assert [content for _, content in reviews] == ["original", "revision 1"]
    assert (paper / "main_round0_original.pdf").read_bytes() == b"%PDF-original"
    assert (paper / "main_round1.pdf").read_bytes() == b"%PDF-revision 1"
    assert (paper / "main_round2.pdf").read_bytes() == (paper / "main.pdf").read_bytes()
    log = (paper / "PAPER_IMPROVEMENT_LOG.md").read_text()
    assert all(item in log for item in ("Review 1", "Fix 1", "Review 2", "Fix 2"))


def test_improvement_entry_and_single_compile_stop(tmp_path, monkeypatch):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("source")
    (paper / "main.pdf").write_bytes(b"%PDF-" + b"0" * 100)
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    state = runner.start_from_project("improvement", "task")
    assert state.current_node == "improvement"
    monkeypatch.setattr(runner, "_compile_pdf", lambda: paper / "main.pdf")
    state = runner.start_from_project("paper_compile", "task", state)
    result = runner.run("task", state=state, stop_after="paper_compile")
    assert result.status == "completed"
    assert list(result.artifacts) == ["paper_compile"]
    assert "improvement" in WORKFLOW_NODE_NAMES
    assert PhaseAgent.SKILL_BY_PHASE["improvement"] == "auto-paper-improvement-loop"
    assert {tool.name for tool in tools_for_phase("improvement", BUILTIN_TOOLS)} >= {
        "read", "edit", "write", "bash", "view_image"
    }


def test_resume_second_round_preserves_first_round(tmp_path, monkeypatch):
    from paper_agent.agent.review_runner import ReviewRunner

    paper = tmp_path / "paper"
    paper.mkdir()
    existing = {
        "main.tex": "revision 1", "main.pdf": "%PDF-round1",
        "main_round0_original.pdf": "%PDF-original", "main_round1.pdf": "%PDF-round1",
        "review_round1.md": "First review", "PAPER_IMPROVEMENT_LOG.md": "First round log\n",
    }
    for name, content in existing.items():
        (paper / name).write_text(content)
    calls = []

    def review(self, request):
        assert self.timeout == 1200
        assert request.output == Path("paper/review_round2.md")
        output = tmp_path / request.output
        output.write_text("Second review")
        return SimpleNamespace(status="completed", report=output)

    monkeypatch.setattr(ReviewRunner, "run", review)

    class Agent:
        project = tmp_path

        def go(self, prompt, **kwargs):
            calls.append(prompt)
            return SimpleNamespace(iterations=1)

    runner = WorkflowRunner(Agent(), config=WorkflowConfig(
        start_node="improvement", improvement_start_round=2))
    monkeypatch.setattr(runner, "_compile_pdf", lambda: paper / "main.pdf")
    result = runner.run("task")
    assert result.status == "completed", result.error
    assert len(calls) == 1
    for name in ("main_round0_original.pdf", "main_round1.pdf", "review_round1.md"):
        assert (paper / name).read_text() == existing[name]
    assert (paper / "PAPER_IMPROVEMENT_LOG.md").read_text().startswith("First round log\n")
    assert (paper / "main_round2.pdf").exists()


def test_structured_resume_reuses_review_and_only_recompiles(tmp_path, monkeypatch):
    import json
    import pytest
    from paper_agent.agent import workflow_cli
    from paper_agent.agent.workflow import WorkflowState
    from paper_agent.agent.review_runner import ReviewRunner

    paper = tmp_path / 'paper'
    paper.mkdir()
    for name in ('main.tex', 'main.pdf', 'main_round0_original.pdf', 'main_round1.pdf',
                 'review_round1.md', 'review_round2.md', 'PAPER_IMPROVEMENT_LOG.md'):
        (paper / name).write_text(name)
    saved = WorkflowState('test', status='failed', current_node='improvement',
                          failed_node='improvement', error='compiler failed', task_prompt='task',
                          improvement_checkpoint={'round': 2, 'stage': 'compile'})
    (tmp_path / 'workflow-state.json').write_text(json.dumps(saved.to_dict()))
    monkeypatch.setattr(workflow_cli, 'PhaseAgent', lambda *a, **k: SimpleNamespace(project=tmp_path))
    monkeypatch.setattr(ReviewRunner, 'run', lambda *a: pytest.fail('must not repeat review'))
    monkeypatch.setattr(WorkflowRunner, '_go', lambda *a, **k: pytest.fail('must not repeat revision'))
    monkeypatch.setattr(WorkflowRunner, '_compile_pdf', lambda self: paper / 'main.pdf')
    result = workflow_cli.run_workflow(tmp_path, 'task', start_node='improvement')
    assert result.status == 'completed', result.error
    assert result.improvement_checkpoint == {'round': 2, 'stage': 'completed'}
    assert (paper / 'main_round0_original.pdf').read_text() == 'main_round0_original.pdf'
    assert (paper / 'main_round1.pdf').read_text() == 'main_round1.pdf'
    assert (paper / 'main_round2.pdf').exists()
