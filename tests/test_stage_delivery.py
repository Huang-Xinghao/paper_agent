"""Flexible delivery contracts retain approval and actual execution evidence."""
import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent.runtime.artifacts import ArtifactVersions
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
    chapter = tmp_path / 'paper/chapters/method.tex'
    chapter.parent.mkdir()
    chapter.write_text('Method')
    bibliography = tmp_path / 'paper/sources.bib'
    bibliography.write_text('@article{source, title={Source}}')
    versions = ArtifactVersions(tmp_path)
    versions.record('paper_write')
    chapter.write_text('Revised method')
    bibliography.write_text('@article{source, title={Corrected source}}')
    assert versions.context()['changed_files']['paper_write'] == [
        'paper/chapters/method.tex', 'paper/sources.bib']


def test_artifact_changes_are_advisory_and_do_not_invalidate_downstream(tmp_path):
    (tmp_path / 'literature').mkdir()
    report = tmp_path / 'literature/RESEARCH_REPORT.md'
    report.write_text('Source A')
    (tmp_path / 'data-search').mkdir()
    (tmp_path / 'data-search/DATA_REPORT.md').write_text('Dataset B')
    versions = ArtifactVersions(tmp_path)
    versions.record('literature')
    versions.record('data_search')
    report.write_text('Source A with a corrected reference')
    context = versions.context()
    assert context['changed_files'] == {'literature': ['literature/RESEARCH_REPORT.md']}
    assert context['nodes']['data_search']['version'] == 1
    assert 'stale_nodes' not in context


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
    with pytest.raises(AwaitingResults): runner._verified_experiment_outputs(state)
    receipts = run_queue(runner.experiments, state, manifest)
    assert len(receipts) == 1 and receipts[0]['status'] == 'completed'
    assert runner._verified_experiment_outputs(state) == [tmp_path / 'results/sample.txt']
    tracker = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert 'sample' in tracker and '执行完成' in tracker
    assert run_queue(runner.experiments, state, manifest) == receipts
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 1
    (tmp_path / 'results/sample.txt').unlink()
    with pytest.raises(AwaitingResults): runner._verified_experiment_outputs(state)


def test_explicit_proposal_revision_pauses_auto_mode_without_old_policy_marker(tmp_path):
    from paper_agent.agent.workflow_cli import refresh_revision_checkpoint
    from paper_agent.proposal_review import pending_refine
    (tmp_path / 'refine-logs').mkdir()
    (tmp_path / 'refine-logs/FINAL_PROPOSAL.md').write_text('Current revised proposal')
    (tmp_path / 'workflow-state.json').write_text(json.dumps({
        'run_id': 'auto', 'project_goal': 'Research', 'mode': 'auto',
        'current_node': 'refine', 'status': 'completed'}))
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
