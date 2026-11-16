"""Flexible delivery contracts retain approval and actual execution evidence."""
import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent.stages import validate_outputs
from paper_agent.agent.workflow import WorkflowConfig, WorkflowRunner, WorkflowState, NodeResult
from paper_agent.tools.experiments.executor import AwaitingResults
from paper_agent.tools.experiments.queue import run_queue


def test_expert_stage_needs_no_checkpoint_sidecar(tmp_path):
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path), config=WorkflowConfig(mode='expert'))
    def deliver(state, prompt):
        report = tmp_path / 'literature/RESEARCH_REPORT.md'
        report.parent.mkdir()
        report.write_text('Evidence, interpretation and open issues in any structure.')
        return NodeResult('literature/RESEARCH_REPORT.md', {})
    runner._nodes['literature'] = deliver
    result = runner.run('Research')
    assert result.status == 'awaiting_user'
    assert result.expert_checkpoint['questions'] == []
    assert result.expert_checkpoint['status'] == 'pending'
    assert not list(tmp_path.rglob('*EXPERT_CHECKPOINT*'))


def test_single_file_manuscript_is_accepted(tmp_path):
    class Agent:
        project = tmp_path
        def go(self, prompt, *, phase, completion_check):
            paper = tmp_path / 'paper'
            paper.mkdir()
            (paper / 'main.tex').write_text('A self-contained manuscript and bibliography.')
            completion_check()
            return SimpleNamespace(iterations=1)
    (tmp_path / 'PAPER_PLAN.md').write_text('Plan')
    (tmp_path / 'figures').mkdir()
    (tmp_path / 'figures/latex_includes.tex').write_text('% no figures needed')
    runner = WorkflowRunner(Agent())
    result = runner._run_paper_write(WorkflowState('single'), 'Write manuscript')
    validate_outputs(tmp_path, 'paper_write')
    assert result.artifact == 'paper/main.tex'
    assert not (tmp_path / 'paper/sections').exists()
    assert not (tmp_path / '.paper-agent/artifact-versions.json').exists()


def test_legacy_experiment_state_drops_duplicate_receipts_and_freeze():
    state = WorkflowState.from_dict({
        'run_id': 'legacy', 'experiment_completed_receipts': [{'large': 'duplicate'}],
        'experiment_feedback': {'error': 'retryable', 'protected_evidence': {'results/a': 'hash'}},
    })
    assert 'experiment_completed_receipts' not in state.to_dict()
    assert 'experiment_feedback' not in state.to_dict()


def test_empty_or_external_artifact_is_still_rejected(tmp_path):
    (tmp_path / 'PAPER_PLAN.md').write_text('  \n')
    with pytest.raises(ValueError, match='empty'): validate_outputs(tmp_path, 'paper_plan')
    (tmp_path / 'PAPER_PLAN.md').unlink()
    (tmp_path / 'PAPER_PLAN.md').symlink_to('/etc/hosts')
    with pytest.raises(ValueError): validate_outputs(tmp_path, 'paper_plan')


def test_freeform_experiment_plan_and_nonnumeric_declared_output(tmp_path, monkeypatch):
    (tmp_path / 'refine-logs').mkdir()
    (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('# Evaluation\nCheck the generated sample.')
    (tmp_path / 'experiments').mkdir()
    # This is a process-boundary fixture, not training or a scientific result.
    (tmp_path / 'experiments/fixture.py').write_text(
        "from pathlib import Path\np=Path('results/sample.txt')\np.parent.mkdir(exist_ok=True)\np.write_text('fixture')\n")
    manifest = {'backend': 'local', 'jobs': [{
        'id': 'sample', 'argv': [sys.executable, 'experiments/fixture.py'],
        'resources': {'device': 'cpu'}, 'timeout_seconds': 5,
        'expected_outputs': [{'path': 'results/sample.txt', 'nonempty': True}],
    }]}
    (tmp_path / 'experiments/manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'project-config.json').write_text(json.dumps({
        'compute': {'device': 'cpu'}, 'execution': {'poll_seconds': .01}}))
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    monkeypatch.setattr(runner.experiments, 'available_devices',
                        lambda: {'cpu': True, 'cuda_count': 0, 'mps': False})
    state = WorkflowState('fixture')
    manifest = runner.experiments.load_manifest()
    # A report or preexisting output alone cannot substitute for an execution receipt.
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/sample.txt').write_text('old artifact')
    assert runner._available_experiment_outputs(state) == []
    receipts = run_queue(runner.experiments, state, manifest)
    assert len(receipts) == 1 and receipts[0]['status'] == 'completed'
    assert runner._available_experiment_outputs(state) == [tmp_path / 'results/sample.txt']
    tracker = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert 'sample' in tracker and '执行完成' in tracker
    # A legacy wrapper removal must not trigger a second fit or evaluation.
    record = next((tmp_path / 'experiment-runs').glob('*/status.json'))
    legacy = json.loads(record.read_text())
    legacy['job']['argv'] = [sys.executable, 'experiments/receipt.py', 'sample', 'cpu',
                             'experiments/fixture.py']
    record.write_text(json.dumps(legacy))
    assert len(run_queue(runner.experiments, state, manifest)) == 1
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 1
    (tmp_path / 'results/sample.txt').unlink()
    assert runner._available_experiment_outputs(state) == []


@pytest.mark.parametrize('mode', ['auto', 'expert'])
def test_proposal_revision_respects_collaboration_mode(tmp_path, mode):
    from paper_agent.agent.workflow_cli import refresh_revision_checkpoint
    from paper_agent.proposal_review import pending_refine
    (tmp_path / 'refine-logs').mkdir()
    (tmp_path / 'refine-logs/FINAL_PROPOSAL.md').write_text('Current revised proposal')
    (tmp_path / 'workflow-state.json').write_text(json.dumps({
        'run_id': 'revision', 'project_goal': 'Research', 'mode': mode,
        'current_node': 'refine', 'status': 'completed'}))
    if mode == 'auto':
        before = (tmp_path / 'workflow-state.json').read_bytes()
        assert not refresh_revision_checkpoint(tmp_path, 'refine')
        assert (tmp_path / 'workflow-state.json').read_bytes() == before
        return
    assert refresh_revision_checkpoint(tmp_path, 'refine')
    saved = json.loads((tmp_path / 'workflow-state.json').read_text())
    assert pending_refine(saved)
    assert 'hpo_policy' not in saved['expert_checkpoint']
    state = WorkflowRunner(SimpleNamespace(project=tmp_path)).resume_checkpoint(WorkflowState.from_dict(saved))
    assert state.current_node == 'experiment_plan' and state.status == 'running'
    assert state.expert_checkpoint['status'] == 'approved'


@pytest.mark.parametrize('channel', [None, 'discussion'])
def test_execution_session_rejects_unscoped_or_wrong_channel_memory(tmp_path, channel):
    from paper_agent.agent.runtime.sessions import RoleSession
    session = RoleSession(tmp_path, 'experiment')
    session.folder.mkdir(parents=True)
    value = {'role': 'experiment', 'messages': [], 'tasks': []}
    if channel is not None: value['channel'] = channel
    session.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='identity mismatch'):
        session.load()


def test_stage_result_survives_later_file_edits_without_workflow_copy(tmp_path, monkeypatch):
    from paper_agent.agent import workflow_cli
    from paper_agent.agent.runtime.execution import new_task, execute_task, read_result
    from paper_agent.storage import write_json
    report = tmp_path / 'PAPER_PLAN.md'
    report.write_text('Original plan')
    state = WorkflowState('source', status='completed', current_node='paper_plan',
                          artifacts={'paper_plan': [{'path': 'PAPER_PLAN.md'}]},
                          node_results={'paper_plan': {'artifact': 'PAPER_PLAN.md'}})
    monkeypatch.setattr(workflow_cli, 'execute_stage', lambda *a, **k: state)
    task = new_task(tmp_path, 'paper_plan', 'Plan', role='paper_writer', project_goal='Research')
    folder = tmp_path / 'task'
    write_json(folder / 'request.json', task.model_dump())
    assert execute_task(folder).status == 'completed'
    assert not (folder / 'workflow.json').exists()
    assert 'state_sha256' not in json.loads((folder / 'result.json').read_text())
    report.write_text('Revised plan')
    result = read_result(folder)
    assert result.status == 'completed'
    assert result.node_results['paper_plan']['artifact'] == 'PAPER_PLAN.md'


def test_bridge_can_report_partial_results_and_retry_after_error(tmp_path, monkeypatch):
    from paper_agent.tools import ToolContext
    (tmp_path / 'refine-logs').mkdir()
    for name in ('FINAL_PROPOSAL.md', 'EXPERIMENT_PLAN.md'):
        (tmp_path / 'refine-logs' / name).write_text('Research plan')
    calls = []
    class Agent:
        project = tmp_path
        def add_tool(self, spec, **kwargs):
            self.tool = spec.handler
        def go(self, prompt, *, phase, completion_check):
            assert self.tool(ToolContext(tmp_path)).is_error
            assert not self.tool(ToolContext(tmp_path)).is_error
            (tmp_path / 'refine-logs/EXPERIMENT_RESULTS.md').write_text(
                'Core experiment completed; an optional audit failed. Limitations documented.')
            completion_check()
            return SimpleNamespace(iterations=2)
    def queue(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('Repairable launch error')
        return []
    monkeypatch.setattr('paper_agent.agent.workflow.run_queue', queue)
    runner = WorkflowRunner(Agent())
    monkeypatch.setattr(runner, '_experiment_manifest', lambda *a: {'jobs': []})
    monkeypatch.setattr(runner.experiments, 'validate_inputs', lambda *a: None)
    result = runner._run_experiment_bridge(WorkflowState('retry'), 'Continue')
    assert result.artifact == 'refine-logs/EXPERIMENT_RESULTS.md'
    assert len(calls) == 2
