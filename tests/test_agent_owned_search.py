"""Search design stays in agent-authored code; execution uses the normal queue."""

import json
from types import SimpleNamespace as NS

import pytest

from paper_agent.agent.workflow import WorkflowRunner, WorkflowState, NodeResult
from paper_agent.tools.experiments.plan import write_experiment_tracker, read_plan_blocks


def plan(project):
    logs = project / 'refine-logs'
    logs.mkdir(exist_ok=True)
    # These fields triggered the original 17-extra-fields failure. They are now
    # research instructions, with no backend search-policy schema.
    policy = {'enabled': True, 'strategy': 'deterministic_exhaustive_nested_grid',
              'block_id': 'B1', 'inner_folds': 2, 'outer_folds': 11,
              'model_metrics': {'ridge': 'validation.event_log_mse'},
              'max_hpo_logical_fits': 528, 'global_threads': 4,
              'incomplete_candidate': 'Exclude incomplete candidates'}
    (logs / 'EXPERIMENT_PLAN.md').write_text(
        '# Plan\n## B1: Nested validation\n'
        'Select independently per model and outer fold.\n'
        '```hpo-policy\n' + json.dumps(policy) + '\n```\n')
    (logs / 'FINAL_PROPOSAL.md').write_text('# Proposal\nUse nested validation.')
    write_experiment_tracker(project, read_plan_blocks(project))


@pytest.mark.parametrize('mode', ['auto', 'expert'])
def test_custom_search_plan_passes_stage_and_existing_approval(tmp_path, mode):
    plan(tmp_path)
    runner = WorkflowRunner(NS(project=tmp_path))
    runner._nodes['experiment_plan'] = lambda state, prompt: NodeResult('refine-logs/EXPERIMENT_PLAN.md')
    state = WorkflowState('custom-policy', current_node='experiment_plan', mode=mode)
    result = runner.run('Research', state=state, stop_after='experiment_plan')
    assert result.error is None
    if mode == 'expert':
        assert result.status == 'awaiting_user'
        result = runner.resume_checkpoint(result)
        assert result.expert_checkpoint['status'] == 'approved'
    assert result.status == 'completed'


def test_custom_search_and_training_use_one_queue_without_backend_binding(tmp_path):
    plan(tmp_path)
    experiments = tmp_path / 'experiments'
    experiments.mkdir()
    # A tiny selection script exercises execution/selection-file wiring, not ML quality.
    (experiments / 'select.py').write_text(
        'from pathlib import Path\nimport json\n'
        'p = Path("results/selection.json")\np.parent.mkdir(exist_ok=True)\n'
        'p.write_text(json.dumps({"outer_fold_1": {"ridge": {"lambda": 0.1}}, '
        '"outer_fold_2": {"ridge": {"lambda": 1.0}}}))\n')
    (experiments / 'train.py').write_text(
        'from pathlib import Path\nimport json\n'
        'selected = json.loads(Path("results/selection.json").read_text())\n'
        'assert selected["outer_fold_1"]["ridge"]["lambda"] == 0.1\n'
        'assert selected["outer_fold_2"]["ridge"]["lambda"] == 1.0\n'
        'Path("results/metrics.json").write_text(json.dumps({"completed_folds": len(selected)}))\n')
    jobs = [
        {'id': 'B1_select', 'block_id': 'B1', 'depends_on': [],
         'argv': ['python', 'experiments/select.py'], 'timeout_seconds': 30, 'resources': {'device': 'cpu'},
         'expected_outputs': [{'path': 'results/selection.json'}]},
        {'id': 'B1_train', 'block_id': 'B1', 'depends_on': ['B1_select'],
         'argv': ['python', 'experiments/train.py'], 'timeout_seconds': 30, 'resources': {'device': 'cpu'},
         'expected_outputs': [{'path': 'results/metrics.json', 'metrics': ['completed_folds']}]},
    ]
    (experiments / 'manifest.json').write_text(json.dumps({'backend': 'local', 'jobs': jobs}))

    class Agent:
        project = tmp_path
        tools = {}

        def add_tool(self, spec, **kwargs):
            self.tools[spec.name] = spec

        def go(self, prompt, **kwargs):
            assert set(self.tools) == {'run_experiments'}
            result = self.tools['run_experiments'].handler(None)
            assert not result.is_error, result.output
            assert json.loads(result.output)['completed_jobs'] == 2
            (tmp_path / 'refine-logs/EXPERIMENT_RESULTS.md').write_text('Verified both folds from actual files.')
            kwargs['completion_check']()
            return NS(iterations=1)

    runner = WorkflowRunner(Agent())
    state = WorkflowState('normal-jobs', current_node='experiment_bridge')
    result = runner._run_experiment_bridge(state, 'Implement the planned search.')
    assert result.metadata['completed_jobs'] == 2
    assert not (tmp_path / 'results/hpo/best_config.json').exists()
    assert json.loads((tmp_path / 'results/metrics.json').read_text()) == {'completed_folds': 2}
    # The backend still enforces the execution/output contract.
    invalid = {'backend': 'local', 'jobs': [{**jobs[0], 'expected_outputs': [{'path': '../escape.json'}]}]}
    with pytest.raises(ValueError, match='escapes workspace'):
        runner.experiments.validate_manifest(invalid)
