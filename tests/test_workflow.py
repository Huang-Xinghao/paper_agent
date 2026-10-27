import json
import sys
from types import SimpleNamespace
import pytest

from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState
from paper_agent.agent.loop import PhaseAgent
from paper_agent.tools import ToolContext


def test_resume_accepts_node_instructions_without_changing_original_task(tmp_path):
    from paper_agent.agent.workflow import NodeResult
    (tmp_path / 'PAPER_PLAN.md').write_text('Existing plan')
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    saved = WorkflowState('existing', task_prompt='Study battery lifetime', status='completed',
                          current_node='experiment_bridge', artifacts={'experiment_bridge': 'results'})
    calls = []

    def handle(state, prompt):
        calls.append((state.current_node, prompt))
        return NodeResult('artifact')

    runner._nodes['paper_figure'] = handle
    runner._nodes['paper_write'] = handle
    runner._next_node = lambda state, node: 'paper_write' if node == 'paper_figure' else None
    state = runner.start_from_project('paper_figure', 'Use three panels and blue colors', saved)
    assert state.task_prompt == 'Study battery lifetime'
    result = runner.run('Use three panels and blue colors', state=state)
    assert result.status == 'completed'
    assert [node for node, _ in calls] == ['paper_figure', 'paper_write']
    assert 'Study battery lifetime' in calls[0][1]
    assert 'Use three panels and blue colors' in calls[0][1]
    assert calls[1][1] == 'Study battery lifetime'
    assert result.node_results['paper_figure']['node_instructions'] == 'Use three panels and blue colors'
    assert result.node_results['paper_write']['node_instructions'] == ''
    restored = WorkflowState.from_dict(result.to_dict())
    assert restored.task_prompt == 'Study battery lifetime'
    assert restored.history[-2]['node_instructions'] == 'Use three panels and blue colors'
    # Another entry with the original task does not inherit previous figure instructions.
    runner.start_from_project('paper_figure', 'Study battery lifetime', restored)
    calls.clear()
    runner.run('Study battery lifetime', state=restored, stop_after='paper_figure')
    assert calls == [('paper_figure', 'Study battery lifetime')]


def test_first_explicit_entry_uses_prompt_as_original_task(tmp_path):
    (tmp_path / 'PAPER_PLAN.md').write_text('Existing plan')
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    state = runner.start_from_project('paper_figure', 'Battery research')
    assert state.task_prompt == 'Battery research'
    assert state.node_instructions == ''


def test_checkpoint_round_trip_preserves_current_state():
    current = WorkflowState(
        "current-run", current_node="paper_figure", status="failed",
        artifacts={"paper_plan": "PAPER_PLAN.md"},
        history=[{"node": "experiment_bridge"}], experiment_repair_count=2,
        experiment_completed_receipts=[{"job_id": "R001"}],
    ).to_dict()
    assert WorkflowState.from_dict(current).to_dict() == current


@pytest.mark.parametrize("field", [
    "review", "experiment_review_status", "revision_count",
    "idea_candidates", "selected_idea", "last_experiment_failure",
    "experiment_expansion_requested", "experiment_expansion_count",
    "misspelled_field",
])
def test_checkpoint_rejects_unknown_fields(field):
    with pytest.raises(TypeError, match="unexpected keyword"):
        WorkflowState.from_dict({"run_id": "test", field: True})


class StubAgent:
    def __init__(self, reviews, project):
        self.phases = []
        self.project = project
        self.knowledge_root = None
        (project / "literature").mkdir(exist_ok=True)
        (project / "literature/RESEARCH_REPORT.md").write_text("Fixture literature findings")
        (project / "data-search").mkdir(exist_ok=True)
        (project / "data-search/DATA_REPORT.md").write_text("Fixture dataset findings")

    def add_tool(self, spec, **kwargs):
        if not hasattr(self, "tools"):
            self.tools = {}
        self.tools[spec.name] = spec

    def go(self, prompt, *, phase=None, completion_check=None):
        self.phases.append(phase)
        if phase == "data_search":
            (self.project / "data-search/DATA_REPORT.md").write_text("Selected dataset and package evidence")
        if phase == "ideas":
            (self.project / "idea-stage").mkdir(exist_ok=True)
            (self.project / "idea-stage/IDEA_REPORT.md").write_text("Test idea")
        if phase in {"proposal", "research_refine"}:
            root = self.project / "refine-logs"
            root.mkdir(exist_ok=True)
            (root / "FINAL_PROPOSAL.md").write_text("Proposal")
        elif phase == "experiment_plan":
            root = self.project / "refine-logs"
            root.mkdir(exist_ok=True)
            (root / "EXPERIMENT_PLAN.md").write_text("### 区块 B1：Main experiment\n- 优先级：必须执行")
            (root / "EXPERIMENT_TRACKER.md").write_text("Experiment tracker")
        elif phase == "experiment_bridge":
            (self.project / "experiments").mkdir(exist_ok=True)
            script = "import pathlib; p=pathlib.Path(__import__('sys').argv[1]); p.parent.mkdir(exist_ok=True); p.write_text('{\"metric\": 0.82}')"
            def job(name, output):
                return {"id": name, "block_id": "B1", "depends_on": [], "argv": [sys.executable, "-c", script, output], "timeout_seconds": 5, "expected_outputs": [{'path': output, 'metrics': ['metric']}]}
            manifest_path = self.project / "experiments/manifest.json"
            if not manifest_path.exists():
                manifest_path.write_text(json.dumps({
                    "backend": "local", "required_inputs": ["data.csv"],
                    "jobs": [job("main", "results/m.json")]}))
            manifest = json.loads(manifest_path.read_text())
            manifest['requirements'] = [dict(id="B1", plan_ref="B1 main result", required=True, job_ids=["main"])]
            manifest_path.write_text(json.dumps(manifest))
            for name in ("run_experiments",):
                result = self.tools[name].handler(ToolContext(self.project))
                if result.is_error:
                    completion_check()
            (self.project / "refine-logs/EXPERIMENT_RESULTS.md").write_text("Measured main result")
            completion_check()
        elif phase == "paper_plan":
            (self.project / "PAPER_PLAN.md").write_text("Claim and evidence plan")
        elif phase == "paper_figure":
            root = self.project / "figures"
            root.mkdir(exist_ok=True)
            (root / "latex_includes.tex").write_text("% no figure required")
            (root / "figure_plan.md").write_text("Figure plan")
        elif phase == "paper_write":
            root = self.project / "paper"
            (root / "sections").mkdir(parents=True, exist_ok=True)
            (root / "main.tex").write_text("\\documentclass{article}")
            (root / "references.bib").write_text("% verified references")
            (root / "sections/intro.tex").write_text("Introduction")
        return SimpleNamespace(final=f"{phase} artifact", iterations=1, messages=[])


class ResearchRecordingAgent(PhaseAgent):
    def __init__(self, project, knowledge_root):
        self.project = project
        self.knowledge_root = knowledge_root
        self.calls = []
        (project / "literature").mkdir(exist_ok=True)
        (project / "literature/RESEARCH_REPORT.md").write_text("Prior battery literature finding")
        (project / "data-search").mkdir(exist_ok=True)
        (project / "data-search/DATA_REPORT.md").write_text("Prior verified dataset finding")

    def go(self, prompt, *, phase=None, completion_check=None):
        self.calls.append((phase, prompt))
        if phase == "ideas":
            target = self.project / "idea-stage/IDEA_REPORT.md"
            target.parent.mkdir(exist_ok=True)
            target.write_text("Battery SOH idea")
        elif phase in {"proposal", "research_refine"}:
            target = self.project / "refine-logs"
            target.mkdir(exist_ok=True)
            (target / "FINAL_PROPOSAL.md").write_text("Proposal")
        return SimpleNamespace(final=f"{phase} artifact", iterations=1)


def test_compute_context_is_injected_once_by_agent(tmp_path, monkeypatch):
    from paper_agent.agent.model_client import ModelResponse, ToolCall

    (tmp_path / "project-config.json").write_text('{"compute": {"device": "cuda", "gpu_index": 1}}')
    (tmp_path / "literature").mkdir()
    (tmp_path / "literature/RESEARCH_REPORT.md").write_text("Prior research")
    (tmp_path / "data-search").mkdir()
    (tmp_path / "data-search/DATA_REPORT.md").write_text("Dataset findings")
    class Model:
        def complete(self, messages, tools):
            system, task = messages[0]["content"], messages[1]["content"]
            assert '\"gpu_index\": 1' in system
            assert '\"gpu_index\": 1' not in task
            if not any(m["role"] == "tool" for m in messages):
                return ModelResponse(tool_calls=(ToolCall("idea", "write", {
                    "path": "idea-stage/IDEA_REPORT.md", "content": "Evidence and hypothesis"}),))
            return ModelResponse(content="Done")

    agent = PhaseAgent(tmp_path, model_client=Model())
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    monkeypatch.setattr(runner, "_research_context", lambda *args, **kwargs: {})
    state = runner.run("battery SOH", stop_after="ideas")
    assert state.status == "completed", state.error


@pytest.mark.parametrize("report", [None, "  "])
def test_ideas_require_nonempty_literature_report(tmp_path, report):
    agent = ResearchRecordingAgent(tmp_path, None)
    path = tmp_path / "literature/RESEARCH_REPORT.md"
    if report is None:
        path.unlink()
    else:
        path.write_text(report)
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    with pytest.raises((FileNotFoundError, ValueError, RuntimeError), match="RESEARCH_REPORT.md"):
        runner._run_ideas(WorkflowState("test"), "Battery SOH")
    assert agent.calls == []


def test_ideas_entry_requires_literature_report(tmp_path):
    agent = ResearchRecordingAgent(tmp_path, None)
    (tmp_path / "literature/RESEARCH_REPORT.md").unlink()
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))

    with pytest.raises((FileNotFoundError, ValueError, RuntimeError), match="RESEARCH_REPORT.md"):
        runner.start_from_project("ideas", "Battery SOH")
    assert agent.calls == []


def test_workflow_waits_for_experiment_inputs(tmp_path):
    agent = StubAgent(["score: 8/10\nverdict: PROCEED"], tmp_path)
    state = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas")).run("Find a research idea")
    assert state.status == "awaiting_results"
    assert state.current_node == "experiment_bridge"
    assert agent.phases == ["ideas", "research_refine", "experiment_plan", "experiment_bridge"]


def test_node_callbacks_report_progress(tmp_path):
    agent = StubAgent(["score: 8\nverdict: PROCEED"], tmp_path)
    events = []
    runner = WorkflowRunner(
        agent,
        on_node_start=lambda node, state: events.append(("start", node)),
        on_node_end=lambda node, state, result: events.append(("end", node)),
        config=WorkflowConfig(start_node="ideas"),
    )
    runner.run("Find a research idea")
    assert events == [("start", "ideas"), ("end", "ideas"),
                      ("start", "refine"), ("end", "refine"),
                      ("start", "experiment_plan"), ("end", "experiment_plan"),
                      ("start", "experiment_bridge")]


class CompilingRunner(WorkflowRunner):
    def _compile_pdf(self):
        pdf = self.agent.project / "paper/main.pdf"
        pdf.write_bytes(b"%PDF-1.4" + b"0" * 100)
        return pdf


def test_missing_paper_artifact_fails_before_next_node(tmp_path):
    agent = StubAgent([], tmp_path)
    (tmp_path / "results").mkdir()
    (tmp_path / "results/main.csv").write_text("metric,value\naccuracy,0.82\n")
    state = WorkflowRunner(agent, config=WorkflowConfig(start_node="paper_plan")).run("Draft")
    assert state.status == "failed"
    assert "FINAL_PROPOSAL.md" in state.error


def test_invalid_results_keep_workflow_paused(tmp_path):
    agent = StubAgent([], tmp_path)
    (tmp_path / "results").mkdir()
    (tmp_path / "results/main.json").write_text('{"status": "planned"}')
    state = WorkflowRunner(agent, config=WorkflowConfig(start_node="paper_plan")).run("Draft")
    assert state.status == "awaiting_results"
    assert not agent.phases


def test_missing_latex_tool_is_repaired_before_retry(tmp_path, monkeypatch):
    agent = StubAgent([], tmp_path)
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper/main.tex").write_text("document")
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    repaired = []

    def repair(prompt, *, phase, completion_check=None):
        assert phase == "paper_compile"
        repaired.append(prompt)
        completion_check()
        return SimpleNamespace(iterations=1)

    def compile_pdf():
        if not repaired:
            raise RuntimeError("latexmk is not installed")
        pdf = tmp_path / "paper/main.pdf"
        pdf.write_bytes(b"%PDF-1.4" + b"0" * 100)
        return pdf

    monkeypatch.setattr(agent, "go", repair)
    monkeypatch.setattr(runner, "_compile_pdf", compile_pdf)
    result = runner._run_paper_compile(None, "Compile")
    assert len(repaired) == 1
    assert (tmp_path / "paper/main.pdf").is_file()


def test_compile_failure_never_marks_paper_complete(tmp_path):
    agent = StubAgent([], tmp_path)
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper/main.tex").write_text("broken")
    state = WorkflowRunner(agent, config=WorkflowConfig(start_node="paper_compile")).run("Compile")
    assert state.status == "failed"
    assert "paper_compile" not in state.artifacts


@pytest.mark.parametrize("returncode", [0, 1])
def test_compile_handles_non_utf8_font_diagnostics(tmp_path, monkeypatch, returncode):
    import subprocess
    import paper_agent.agent.workflow as workflow

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text("document")
    (paper / "main.pdf").write_bytes(b"%PDF-1.4" + b"0" * 100)
    run = subprocess.run

    def latex_run(argv, **kwargs):
        return run([sys.executable, "-c",
                    f"import os; os.write(1, b'font warning: \\x87'); os.write(2, b'error: \\xff'); raise SystemExit({returncode})"],
                   **kwargs)

    monkeypatch.setattr(workflow.shutil, "which", lambda name: "/fake/latexmk")
    monkeypatch.setattr(workflow.subprocess, "run", latex_run)
    runner = WorkflowRunner(StubAgent([], tmp_path))
    if returncode:
        with pytest.raises(RuntimeError, match="latexmk failed:.*font warning"):
            runner._compile_pdf()
    else:
        assert runner._compile_pdf() == paper / "main.pdf"


def test_experiment_failure_preserves_log_and_blocks_paper(tmp_path):
    agent = StubAgent(['score: 8\nverdict: PROCEED'], tmp_path)
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    state = runner.run('Research')
    (tmp_path / 'data.csv').write_text('fixture')
    manifest = json.loads((tmp_path / 'experiments/manifest.json').read_text())
    manifest['jobs'][0]['argv'] = [sys.executable, '-c', 'raise RuntimeError("training failed")']
    (tmp_path / 'experiments/manifest.json').write_text(json.dumps(manifest))
    state = runner.run('Research', state=state)
    assert state.status == 'failed'
    assert state.current_node == 'experiment_bridge'
    assert 'paper_plan' not in agent.phases
    logs = list((tmp_path / 'experiment-runs').glob('main-*/output.log'))
    assert 'training failed' in logs[0].read_text()


def test_experiment_timeout_and_stale_output(tmp_path):
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    (tmp_path / 'results').mkdir()
    output = tmp_path / 'results/old.json'
    output.write_text('{"metric": 1}')
    job = {'id': 'timeout', 'argv': [sys.executable, '-c', 'import time; time.sleep(5)'],
           'timeout_seconds': 0.05, 'expected_outputs': [{'path': 'results/old.json', 'metrics': ['metric']}]}
    with pytest.raises(RuntimeError, match='failed'):
        runner.experiments.execute(job)
    assert not output.exists()
    assert list((tmp_path / 'experiment-runs').glob('*/previous-0.json'))
    record = json.loads(next((tmp_path / 'experiment-runs').glob('*/status.json')).read_text())
    assert record['status'] == 'failed'


def test_experiment_resources_are_passed_and_recorded(tmp_path, monkeypatch):
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    monkeypatch.setattr(runner.experiments, 'available_devices', lambda: {'cpu': True, 'cuda_count': 0, 'mps': False})
    (tmp_path / 'experiments').mkdir()
    (tmp_path / 'project-config.json').write_text(json.dumps({'compute': {'device': 'cpu', 'cpu_threads': 2}}))
    script = ('import json,os,pathlib; '
              'p=pathlib.Path("results/device.json"); p.parent.mkdir(exist_ok=True); '
              'p.write_text(json.dumps({"metric":1,"device":os.environ["PAPER_AGENT_DEVICE"],'
              '"threads":os.environ["OMP_NUM_THREADS"],"cuda":os.environ["CUDA_VISIBLE_DEVICES"]}))')
    job = {'id': 'cpu', 'argv': [sys.executable, '-c', script],
           'timeout_seconds': 5, 'expected_outputs': [{'path': 'results/device.json', 'metrics': ['metric']}]}
    receipt = runner.experiments.execute(job)
    assert receipt['resources']['allocated_device'] == 'cpu'
    assert receipt['resources']['cpu_threads'] == 2
    result = json.loads((tmp_path / 'results/device.json').read_text())
    assert result == {'metric': 1, 'device': 'cpu', 'threads': '2', 'cuda': ''}
    record = json.loads(next((tmp_path / 'experiment-runs').glob('*/status.json')).read_text())
    assert record['resources'] == receipt['resources']


def test_missing_required_gpu_pauses_without_running_or_repair(tmp_path, monkeypatch):
    agent = StubAgent(['score: 8\nverdict: PROCEED'], tmp_path)
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    monkeypatch.setattr(runner.experiments, 'available_devices', lambda: {'cpu': True, 'cuda_count': 0, 'mps': False})
    state = runner.run('Research')
    (tmp_path / 'data.csv').write_text('fixture')
    manifest_path = tmp_path / 'experiments/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['jobs'][0]['resources'] = {'device': 'cuda'}
    manifest_path.write_text(json.dumps(manifest))
    state = runner.run('Research', state=state)
    assert state.status == 'awaiting_results'
    assert state.current_node == 'experiment_bridge'
    assert 'requires CUDA' in state.error
    assert 'experiment_repair' not in agent.phases
    assert not (tmp_path / 'experiment-runs').exists()


def test_gpu_index_with_auto_does_not_fall_back_to_cpu(tmp_path, monkeypatch):
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    monkeypatch.setattr(runner.experiments, 'available_devices', lambda: {'cpu': True, 'cuda_count': 0, 'mps': False})
    (tmp_path / 'experiments').mkdir()
    (tmp_path / 'project-config.json').write_text('{"compute": {"gpu_index": 1}}')
    with pytest.raises(Exception, match='requires CUDA GPU 1'):
        runner.experiments.resolve_resources({'id': 'main'})


@pytest.mark.parametrize('config', [
    {'device': 'gpu'}, {'device': 'cpu', 'gpu_index': 0},
    {'device': 'cuda', 'gpu_index': -1}, {'cpu_threads': 0},
])
def test_invalid_resource_configuration_is_rejected(tmp_path, config):
    (tmp_path / 'experiments').mkdir()
    (tmp_path / 'project-config.json').write_text(json.dumps({'compute': config}))
    with pytest.raises(ValueError):
        WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas")).experiments.compute_config()


def test_invalid_numeric_results_rejected(tmp_path):
    import pytest
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    (tmp_path / 'results').mkdir()
    for content in ['{"metric": NaN}', '{"metric": "Infinity"}', '{"status": "done"}']:
        (tmp_path / 'results/m.json').write_text(content)
        with pytest.raises(ValueError):
            runner.experiments.validate_outputs({'id': 'test', 'expected_outputs': [{'path': 'results/m.json', 'metrics': ['metric']}]})


@pytest.mark.parametrize('suffix', ['json', 'csv'])
def test_identifier_is_not_a_numeric_measurement(tmp_path, suffix):
    runner = WorkflowRunner(StubAgent([], tmp_path))
    (tmp_path / 'results').mkdir()
    path = tmp_path / f'results/m.{suffix}'
    identifier = '00134112e38313220056'
    for metric in ['1.0', 'NaN', 'Infinity']:
        if suffix == 'json':
            path.write_text(json.dumps({'system_id': identifier, 'metric': metric}))
        else:
            path.write_text(f'system_id,metric\n{identifier},{metric}\n')
        if metric == '1.0':
            runner.experiments.validate_outputs({'id': 'test', 'expected_outputs': [{'path': f'results/m.{suffix}', 'metrics': ['metric']}]})
        else:
            with pytest.raises(ValueError):
                runner.experiments.validate_outputs({'id': 'test', 'expected_outputs': [{'path': f'results/m.{suffix}', 'metrics': ['metric']}]})
    path.write_text(json.dumps({'system_id': identifier}) if suffix == 'json'
                    else f'system_id\n{identifier}\n')
    with pytest.raises(ValueError):
        runner.experiments.validate_outputs({'id': 'test', 'expected_outputs': [{'path': f'results/m.{suffix}', 'metrics': ['metric']}]})


def test_experiments_continue_directly_to_paper_plan(tmp_path):
    agent = StubAgent(['score: 8\nverdict: PROCEED'], tmp_path)
    (tmp_path / 'data.csv').write_text('fixture')
    runner = WorkflowRunner(agent, config=WorkflowConfig(start_node="ideas"))
    state = runner.run('Research', stop_after='paper_plan')
    assert state.status == 'completed'
    assert state.experiment_completed_receipts[0]['status'] == 'completed'
    nodes = [entry['node'] for entry in state.history if 'metadata' in entry]
    assert nodes[nodes.index('experiment_bridge') + 1] == 'paper_plan'
    assert 'experiment_check' not in nodes and 'experiment_run' not in nodes
    assert agent.phases[-1] == 'paper_plan'


def test_paper_plan_entry_requires_no_audit_artifact(tmp_path):
    root = tmp_path / 'refine-logs'
    root.mkdir()
    (root / 'FINAL_PROPOSAL.md').write_text('Proposal')
    (root / 'EXPERIMENT_PLAN.md').write_text('Plan')
    runner = WorkflowRunner(StubAgent([], tmp_path), config=WorkflowConfig(start_node="ideas"))
    state = runner.start_from_project('paper_plan', 'Research')
    assert state.current_node == 'paper_plan'
