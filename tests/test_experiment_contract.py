"""Execution stays usable without experiment handoff gates."""
import json
import pytest

from paper_agent.agent.model_client import ModelResponse
from test_bridge_orchestration import setup_project, runner_for, call, write


def save_manifest(root, manifest):
    (root / 'experiments/manifest.json').write_text(json.dumps(manifest))


def test_run_without_requirements(tmp_path):
    manifest, _, _ = setup_project(tmp_path)
    del manifest['requirements']
    save_manifest(tmp_path, manifest)
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Measured result'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert len(result.experiment_completed_receipts) == 1


def test_manifest_expansion_runs_without_rechecking_and_reuses_jobs(tmp_path):
    manifest, _, job = setup_project(tmp_path)
    expanded = {**manifest, 'jobs': manifest['jobs'] + [job('statistics', 'results/stats.json')]}
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('experiments/manifest.json', expanded), call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Measured result'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert len(result.experiment_completed_receipts) == 2
    assert (tmp_path / 'results/train_count.txt').read_text() == '1'


def test_handoff_ignores_requirement_gaps_and_source_edits(tmp_path):
    manifest, _, _ = setup_project(tmp_path)
    manifest['requirements'] = [{'id': 'missing', 'required': True, 'job_ids': []}]
    save_manifest(tmp_path, manifest)
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('experiments/job.py', 'raise RuntimeError("future edit")'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Results and remaining gaps'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error


def test_exit_zero_with_missing_artifact_returns_failure_and_can_be_repaired(tmp_path):
    manifest, _, _ = setup_project(tmp_path)
    manifest['jobs'][0]['expected_outputs'].append({'path': 'results/missing.json', 'metrics': ['mae']})
    save_manifest(tmp_path, manifest)
    good = (tmp_path / 'experiments/job.py').read_text()
    repair = good + "\npathlib.Path('results/missing.json').write_text(json.dumps({'mae': 0.5}))\n"
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('experiments/job.py', repair), call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Repaired missing artifact'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert result.experiment_completed_receipts[0]['exit_code'] == 0
    assert result.experiment_completed_receipts[0]['status'] == 'completed'
    assert result.experiment_repair_count == 1
    attempts = [json.loads(p.read_text()) for p in (tmp_path / 'experiment-runs').glob('*/status.json')]
    failed = next(r for r in attempts if r['status'] == 'failed')
    assert failed['exit_code'] == 0 and 'results/missing.json' in failed['error']
    assert (tmp_path / 'results/missing.json').exists()


@pytest.mark.parametrize('content,suffix', [('[]', 'json'), ('{}', 'json'), ('metric\n', 'csv')])
def test_empty_collections_are_valid_unless_nonempty_or_metrics_required(tmp_path, content, suffix):
    from paper_agent.agent.experiment_executor import ExperimentExecutor
    manifest, _, _ = setup_project(tmp_path)
    executor = ExperimentExecutor(tmp_path)
    output = {'path': f'results/empty.{suffix}'}
    job = manifest['jobs'][0]
    job['expected_outputs'] = [output]
    target = tmp_path / output['path']
    target.parent.mkdir()
    target.write_text(content)
    assert not executor.output_issues(job)
    output['nonempty'] = True
    save_manifest(tmp_path, manifest)
    executor.load_manifest()
    assert 'nonempty' in executor.output_issues(job)[0]['problem']
    output['nonempty'] = False
    output['metrics'] = ['metric']
    assert executor.output_issues(job)
    output['nonempty'] = 'true'
    save_manifest(tmp_path, manifest)
    with pytest.raises(ValueError, match='nonempty must be a boolean'):
        executor.load_manifest()


@pytest.mark.parametrize('content', ['null', '1', '"text"', '{broken', ''])
def test_json_outputs_still_require_valid_objects_or_arrays(tmp_path, content):
    from paper_agent.agent.experiment_executor import ExperimentExecutor
    manifest, _, _ = setup_project(tmp_path)
    job = manifest['jobs'][0]
    job['expected_outputs'] = [{'path': 'results/value.json'}]
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/value.json').write_text(content)
    assert ExperimentExecutor(tmp_path).output_issues(job)


def test_tracker_retains_execution_when_upstream_artifact_becomes_invalid(tmp_path):
    from paper_agent.agent.experiment_queue import run_queue
    manifest, _, job = setup_project(tmp_path)
    manifest['jobs'].append({**job('stats', 'results/stats.json'), 'depends_on': ['train']})
    runner, _, state = runner_for(tmp_path, [])
    run_queue(runner.experiments, state, manifest)
    (tmp_path / 'results/train.json').unlink()
    runner._refresh_experiment_tracker(state, manifest)
    text = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert '| 产物校验失败 | 0/2 | 0 | 1 | 2/2 |' in text
    assert '| train | 产物校验失败 | results/train.json:' in text
    assert '| stats | 依赖产物无效 | 依赖未通过校验：train |' in text
    # Restoring the valid artifact restores completion without another execution.
    (tmp_path / 'results/train.json').write_text('{"mae": 0.5}')
    runner._refresh_experiment_tracker(state, manifest)
    text = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert '| 执行完成 | 2/2 | 0 | 0 | 2/2 |' in text
    assert (tmp_path / 'results/train_count.txt').read_text() == '1'


def test_handoff_does_not_require_execution_claim_verification(tmp_path):
    setup_project(tmp_path)
    runner, _, state = runner_for(tmp_path, [
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Agent assessment'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert not result.experiment_completed_receipts


def test_failed_command_still_returns_failure_and_can_be_repaired(tmp_path):
    setup_project(tmp_path)
    good = (tmp_path / 'experiments/job.py').read_text()
    (tmp_path / 'experiments/job.py').write_text('raise RuntimeError("training failed")')
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('experiments/job.py', good), call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Repaired result'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert result.experiment_repair_count == 1
    receipts = [json.loads(p.read_text()) for p in (tmp_path / 'experiment-runs').glob('*/status.json')]
    assert {r['status'] for r in receipts} == {'failed', 'completed'}


def test_plan_initializes_tracker_without_agent_authored_tracker(tmp_path):
    from paper_agent.agent.workflow import WorkflowRunner
    from types import SimpleNamespace
    root = tmp_path / 'refine-logs'
    root.mkdir()
    (root / 'EXPERIMENT_PLAN.md').write_text(
        '### 区块 D1：数据展示\n- 优先级：必须执行\n'
        '### 区块 B1：总体性能\n- 优先级：NICE-TO-HAVE\n')
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    runner._validate_phase_outputs('experiment_plan')
    text = (root / 'EXPERIMENT_TRACKER.md').read_text()
    assert '| D1 | 数据展示 | 必须执行 | 尚未部署 | 0/0 |' in text
    assert '| B1 | 总体性能 | NICE-TO-HAVE | 尚未部署 | 0/0 |' in text


def test_manifest_requires_known_block(tmp_path):
    import pytest
    from paper_agent.agent.experiment_executor import ExperimentExecutor
    manifest, _, _ = setup_project(tmp_path)
    executor = ExperimentExecutor(tmp_path)
    save_manifest(tmp_path, manifest)
    assert executor.load_manifest()['jobs'][0]['block_id'] == 'B1'
    for value in (None, '', 'B99', ['B1']):
        manifest['jobs'][0]['block_id'] = value
        save_manifest(tmp_path, manifest)
        with pytest.raises(ValueError, match='block_id'):
            executor.load_manifest()
    del manifest['jobs'][0]['block_id']
    save_manifest(tmp_path, manifest)
    with pytest.raises(ValueError, match='block_id'):
        executor.load_manifest()


def test_tracker_keeps_undeployed_blocks_and_counts_failures(tmp_path):
    from paper_agent.agent.experiment_handoff import read_plan_blocks, write_experiment_tracker
    root = tmp_path / 'refine-logs'
    root.mkdir()
    (root / 'EXPERIMENT_PLAN.md').write_text(
        '### 区块 D1：数据展示\n### 区块 B1：性能\n### 区块 B2：消融\n')
    jobs = [dict(id=name, block_id='B1') for name in ('a', 'b', 'c', 'd')]
    states = [dict(id=name, status=status) for name, status in
              [('a', 'completed'), ('b', 'failed'), ('c', 'blocked'), ('d', 'running')]]
    write_experiment_tracker(tmp_path, read_plan_blocks(tmp_path), jobs, states)
    text = (root / 'EXPERIMENT_TRACKER.md').read_text()
    assert '| B1 | 性能 | 必须执行 | 存在失败 | 1/4 | 1 | 1 |' in text
    assert '| B2 | 消融 | 必须执行 | 尚未部署 | 0/0 | 0 | 0 |' in text
    assert '| D1 | 数据展示 | 必须执行 | 尚未部署 | 0/0 | 0 | 0 |' in text


def test_plan_rejects_duplicate_or_absent_block_ids(tmp_path):
    import pytest
    from paper_agent.agent.experiment_handoff import read_plan_blocks
    root = tmp_path / 'refine-logs'
    root.mkdir()
    path = root / 'EXPERIMENT_PLAN.md'
    path.write_text('### 区块 B1：性能\n### 区块 B1：另一个实验\n')
    with pytest.raises(ValueError, match='Duplicate'):
        read_plan_blocks(tmp_path)
    path.write_text('No block headings')
    with pytest.raises(ValueError, match='requires block headings'):
        read_plan_blocks(tmp_path)
    path.write_text('```markdown\n### 区块 B1：example\n```\n### 区块 D1：Actual\n')
    assert [block['id'] for block in read_plan_blocks(tmp_path)] == ['D1']


def test_only_run_tool_is_registered_during_bridge(tmp_path):
    setup_project(tmp_path)
    def inspect_tools(messages, tools):
        names = {tool['function']['name'] for tool in tools}
        assert 'run_experiments' in names
        assert 'check_experiments' not in names
        return call('run_experiments')
    runner, _, state = runner_for(tmp_path, [inspect_tools,
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Measured result'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 1


def test_missing_input_can_be_supplied_before_retrying_formal_run(tmp_path):
    setup_project(tmp_path)
    (tmp_path / 'data.csv').unlink()
    runner, _, state = runner_for(tmp_path, [call('run_experiments'),
        write('data.csv', 'restored fixture input'), call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Measured result'), ModelResponse(content='Done')])
    result = runner.run('task', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert result.experiment_repair_count == 0
    assert len(result.experiment_completed_receipts) == 1
