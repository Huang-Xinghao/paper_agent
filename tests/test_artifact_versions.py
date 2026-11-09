import json
from types import SimpleNamespace

import pytest

from paper_agent.agent.artifact_versions import ArtifactVersions
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState, NodeResult
from paper_agent.agent.workflow_cli import _write_json


def outputs(project):
    paths = {'refine': 'refine-logs/FINAL_PROPOSAL.md',
             'experiment_plan': 'refine-logs/EXPERIMENT_PLAN.md',
             'experiment_bridge': 'refine-logs/EXPERIMENT_RESULTS.md',
             'paper_plan': 'PAPER_PLAN.md', 'paper_write': 'paper/main.tex'}
    for node, path in paths.items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(node + ' v1')
    return paths


def test_proposal_revision_invalidates_downstream_and_prevents_old_results_use(tmp_path):
    paths = outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    versions.prepare('refine')
    (tmp_path / paths['refine']).write_text('proposal v2')
    data = versions.record('refine', files=[paths['refine']])
    assert data['nodes']['refine']['version'] == 2
    assert set(data['stale_nodes']) == {'experiment_plan', 'experiment_bridge', 'paper_plan', 'paper_write',
                                       'paper_compile', 'improvement'}
    # Old downstream files are preserved for comparison, but cannot be accepted.
    assert (tmp_path / paths['experiment_plan']).read_text() == 'experiment_plan v1'
    with pytest.raises(ValueError, match='experiment_plan'):
        versions.prepare('experiment_bridge')
    versions.prepare('experiment_plan')
    (tmp_path / paths['experiment_plan']).write_text('plan based on proposal v2')
    versions.record('experiment_plan', regenerated=True)
    versions.prepare('experiment_bridge')
    assert 'experiment_bridge' in versions.context()['stale_nodes']
    versions.record('experiment_bridge', regenerated=True)
    assert 'experiment_bridge' not in versions.context()['stale_nodes']
    assert 'paper_plan' in versions.context()['stale_nodes']


def test_noop_revision_does_not_invalidate_downstream(tmp_path):
    outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    original = versions.prepare('refine')
    result = versions.record('refine')
    assert result['nodes']['refine']['version'] == original['nodes']['refine']['version']
    assert result['stale_nodes'] == {}


def test_external_edits_are_detected_by_status_without_writes_and_block_execution(tmp_path):
    paths = outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    versions.prepare('refine')
    original = versions.path.read_bytes()
    (tmp_path / paths['refine']).write_text('external proposal edit')
    data = versions.context()
    assert 'refine' in data['stale_nodes'] and 'paper_write' in data['stale_nodes']
    assert versions.path.read_bytes() == original
    with pytest.raises(ValueError, match='refine'):
        WorkflowRunner(SimpleNamespace(project=tmp_path)).start_from_project('experiment_bridge', 'Research')


def test_old_plan_cannot_be_approved_after_proposal_changed(tmp_path):
    paths = outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    versions.prepare('refine')
    (tmp_path / paths['refine']).write_text('proposal v2')
    versions.record('refine')
    state = WorkflowState('test', status='awaiting_user', current_node='experiment_plan',
                          expert_checkpoint={'status': 'pending', 'node': 'experiment_plan',
                                             'report': paths['experiment_plan'], 'next_node': 'experiment_bridge'})
    with pytest.raises(ValueError, match='experiment_plan'):
        WorkflowRunner(SimpleNamespace(project=tmp_path)).resume_checkpoint(state)
    assert state.expert_checkpoint['status'] == 'pending'


def test_verified_regeneration_clears_only_the_current_stale_node(tmp_path):
    paths = outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    versions.prepare('refine')
    (tmp_path / paths['refine']).write_text('proposal v2')
    versions.record('refine')
    # Same output can still be valid after deliberate full revalidation.
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    runner._nodes['experiment_plan'] = lambda state, prompt: NodeResult(paths['experiment_plan'])
    state = WorkflowState('test', project_goal='Research', current_node='experiment_plan')
    result = runner.run('Research', state=state, stop_after='experiment_plan')
    assert result.status == 'completed', result.error
    data = versions.context()
    assert 'experiment_plan' not in data['stale_nodes']
    assert 'experiment_bridge' in data['stale_nodes']


def test_confirming_manual_edit_records_new_version_and_invalidates_existing_downstream(tmp_path):
    paths = outputs(tmp_path)
    versions = ArtifactVersions(tmp_path)
    versions.prepare('refine')
    (tmp_path / paths['refine']).write_text('user edited proposal')
    versions.prepare('refine', approval=True, accepted_files=[paths['refine']])
    versions.record('refine', regenerated=True)
    assert versions.context()['nodes']['refine']['version'] == 2
    assert 'experiment_plan' in versions.context()['stale_nodes']
