import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_agent.agent import experiment_queue as queue
from paper_agent.agent.workflow import AwaitingResults, ExperimentExecutionError, WorkflowRunner, WorkflowState
from paper_agent.project_config import load_execution_config, load_compute_config


def setup_queue(tmp_path, monkeypatch, **execution):
    (tmp_path / 'project-config.json').write_text(json.dumps({
        'compute': {'device': 'auto'},
        'execution': {'poll_seconds': .005,
                      'oom_retry_delay_seconds': .01, **execution}}))
    (tmp_path / 'refine-logs').mkdir(exist_ok=True)
    (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Test block\n- 优先级：必须执行')
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    runner.experiments.available_devices = lambda: dict(cpu=True, cuda_count=2, mps=False)
    monkeypatch.setattr(queue, 'discover_gpus', lambda r: {
        0: dict(token='GPU-A', memory_used_mib=0), 1: dict(token='GPU-B', memory_used_mib=0)})
    return runner


def job(identifier, dependencies=(), device='cuda', code=None):
    output = f'results/{identifier}.json'
    code = code or f"from pathlib import Path; Path('results').mkdir(exist_ok=True); Path('{output}').write_text('{{\"metric\":1}}')"
    return dict(id=identifier, block_id='B1', depends_on=list(dependencies), resources={'device': device},
                argv=[sys.executable, '-c', code], timeout_seconds=5, expected_outputs=[{'path': output, 'metrics': ['metric']}])


def run(runner, jobs, state=None):
    return queue.run_queue(runner.experiments, state or WorkflowState('test'), {'jobs': jobs},
                           on_progress=runner.on_node_progress)


def test_parallel_gpu_assignment_and_success_dependencies(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    events = []
    def execute(j, *, resources, cancel_event):
        events.append((j['id'], resources['gpu_index']))
        if j['id'] != 'evaluate':
            barrier.wait(timeout=2)
        else:
            assert {e[0] for e in events} == {'a', 'b', 'evaluate'}
        p = tmp_path / j['expected_outputs'][0]['path']; p.parent.mkdir(exist_ok=True); p.write_text('{"x":1}')
        return dict(job=j, status='completed')
    r.experiments.execute = execute
    jobs = [job('a'), job('b'), job('evaluate', ['a', 'b'], 'cpu')]
    receipts = run(r, jobs)
    assert {gpu for name, gpu in events if name != 'evaluate'} == {0, 1}
    assert [x['job']['id'] for x in receipts] == ['a', 'b', 'evaluate']
    assert json.loads((tmp_path / 'experiment-queue-state.json').read_text())['status'] == 'completed'


def test_real_subprocess_visibility_and_output_validation(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    jobs = [job('a', code="import os,json,time; from pathlib import Path; time.sleep(.1); Path('results').mkdir(exist_ok=True); Path('results/a.json').write_text(json.dumps({'metric':1,'gpu':os.environ['CUDA_VISIBLE_DEVICES']}))"), job('b')]
    receipts = run(r, jobs)
    assert {x['resources']['cuda_visible_device'] for x in receipts} == {'GPU-A', 'GPU-B'}
    assert json.loads((tmp_path / 'results/a.json').read_text())['gpu'] == 'GPU-A'
    assert all(x['exit_code'] == 0 for x in receipts)


def test_missing_output_fails_queue_and_blocks_dependents(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    jobs = [job('producer', code='pass'), job('consumer', ['producer'])]
    with pytest.raises(ExperimentExecutionError, match='file missing or empty'):
        run(r, jobs)
    snapshot = json.loads((tmp_path / 'experiment-queue-state.json').read_text())
    assert snapshot['status'] == 'failed'
    assert [j['status'] for j in snapshot['jobs']] == ['artifact_invalid', 'blocked']
    assert snapshot['jobs'][0]['execution_status'] == 'completed'
    tracker = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert '| 产物校验失败 | 0/2 | 0 | 1 | 1/2 |' in tracker
    assert 'results/producer.json' in tracker
    assert not (tmp_path / 'results/consumer.json').exists()


def test_failure_blocks_evaluation_and_drains_active_jobs(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    calls = []
    def execute(j, **kwargs):
        calls.append(j['id'])
        if j['id'] == 'bad':
            raise ExperimentExecutionError('bad config')
        time.sleep(.03)
        return dict(job=j, status='completed')
    r.experiments.execute = execute
    state = WorkflowState('test')
    with pytest.raises(ExperimentExecutionError):
        run(r, [job('bad'), job('good'), job('evaluate', ['bad', 'good'], 'cpu')], state)
    assert calls == ['bad', 'good']
    assert [x['job']['id'] for x in state.experiment_completed_receipts] == ['good']
    statuses = {j['id']: j['status'] for j in json.loads((tmp_path / 'experiment-queue-state.json').read_text())['jobs']}
    assert statuses == {'bad': 'failed', 'good': 'completed', 'evaluate': 'blocked'}
    tracker = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert '| B1 | Test block | 必须执行 | 存在失败 | 1/3 | 1 | 1 |' in tracker


def test_oom_retry_is_bounded_and_non_oom_is_not_retried(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch, oom_max_retries=1)
    attempts = []
    def execute(j, **kwargs):
        attempts.append(j['id'])
        raise ExperimentExecutionError('CUDA out of memory')
    r.experiments.execute = execute
    with pytest.raises(ExperimentExecutionError):
        run(r, [job('a')])
    assert attempts == ['a', 'a']


def test_resume_validates_outputs_and_invalidates_dependents(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    jobs = [job('a'), job('b', ['a'])]
    state = WorkflowState('test')
    run(r, jobs, state)
    def never(*args, **kwargs):
        raise AssertionError('completed job reran')
    original = r.experiments.execute
    r.experiments.execute = never
    run(r, jobs, state)
    (tmp_path / 'results/a.json').unlink()
    calls = []
    def execute(j, **kwargs):
        calls.append(j['id'])
        return original(j, **kwargs)
    r.experiments.execute = execute
    run(r, jobs, state)
    assert calls == ['a', 'b']


def test_busy_gpu_is_skipped(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(queue, 'discover_gpus', lambda r: {
        0: dict(token='GPU-A', memory_used_mib=2000), 1: dict(token='GPU-B', memory_used_mib=0)})
    assert run(r, [job('a')])[0]['resources']['gpu_index'] == 1


@pytest.mark.parametrize('jobs', [[job('a', ['missing'])], [job('a', ['b']), job('b', ['a'])]])
def test_invalid_dependencies(jobs):
    with pytest.raises(ValueError):
        queue.job_dependencies(jobs)


@pytest.mark.parametrize("missing_index", [0, 1])
def test_missing_dependencies_are_rejected(missing_index):
    jobs = [job('a'), job('b')]
    del jobs[missing_index]['depends_on']
    with pytest.raises(ValueError, match='depends_on'):
        queue.job_dependencies(jobs)


@pytest.mark.parametrize('config', [{'gpu_ids': [0, 0]}, {'gpu_ids': True},
    {'max_parallel_gpu_jobs': 0}, {'oom_max_retries': -1}, {'poll_seconds': float('nan')}])
def test_execution_config_validation(tmp_path, config):
    (tmp_path / 'project-config.json').write_text(json.dumps({'execution': config}))
    with pytest.raises(ValueError):
        load_execution_config(tmp_path)


def test_discovery_respects_inherited_visible_devices(monkeypatch):
    r = SimpleNamespace(available_devices=lambda: {'cuda_count': 2})
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '3,1')
    monkeypatch.setattr(queue.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='0, GPU-A, 10\n1, GPU-B, 20\n2, GPU-C, 30\n3, GPU-D, 40\n'))
    devices = queue.discover_gpus(r)
    assert devices[0]['token'] == 'GPU-D'
    assert devices[1]['token'] == 'GPU-B'


def test_budgeted_queue_keeps_cumulative_budget(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    p = tmp_path / 'project-config.json'; d = json.loads(p.read_text()); d['compute']['total_experiment_seconds'] = .05; p.write_text(json.dumps(d))
    with pytest.raises(ExperimentExecutionError):
        run(r, [job('a', code='import time; time.sleep(1)'), job('b')])
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 1


def test_same_project_queue_cannot_start_twice(tmp_path, monkeypatch):
    import fcntl
    r = setup_queue(tmp_path, monkeypatch)
    (tmp_path / '.paper-agent').mkdir()
    with (tmp_path / '.paper-agent/experiment-executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AwaitingResults, match='already executing'):
            run(r, [job('a')])


def test_no_cuda_stops_instead_of_cpu_fallback(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    r.experiments.available_devices = lambda: dict(cpu=True, cuda_count=0, mps=False)
    monkeypatch.setattr(queue, 'discover_gpus', lambda r: {})
    with pytest.raises(AwaitingResults, match='CUDA'):
        run(r, [job('a')])
    assert not (tmp_path / 'experiment-runs').exists()


def test_success_receipts_recover_without_workflow_checkpoint(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    jobs = [job('a')]
    run(r, jobs)
    r.experiments.execute = lambda *a, **k: pytest.fail('durable completed receipt was not recovered')
    assert run(r, jobs, WorkflowState('fresh'))[0]['status'] == 'completed'


def test_interrupt_cancels_workers_and_records_attempts(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    def interrupt(node, state, details):
        if details['round'] == 2:
            raise KeyboardInterrupt()
    r.on_node_progress = interrupt
    with pytest.raises(KeyboardInterrupt):
        run(r, [job('a', code='import time; time.sleep(20)')])
    receipts = [json.loads(p.read_text()) for p in (tmp_path / 'experiment-runs').glob('*/status.json')]
    assert receipts and all(r['status'] == 'interrupted' for r in receipts)


def test_workflow_without_execution_config_uses_queue_and_revalidates_resume(tmp_path, monkeypatch):
    """Default configuration must preserve explicit dependencies and recovery rules."""
    (tmp_path / 'refine-logs').mkdir(exist_ok=True)
    (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Test block\n- 优先级：必须执行')
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    runner.experiments.available_devices = lambda: dict(cpu=True, cuda_count=0, mps=False)
    defaults = load_execution_config(tmp_path)
    monkeypatch.setattr(queue, 'load_execution_config', lambda project: defaults | {'poll_seconds': .005})
    first = job('a', device='cpu')
    second = job('b', device='cpu', code=(
        "from pathlib import Path; import json; "
        "metric=json.loads(Path('results/a.json').read_text())['metric']; "
        "Path('results/b.json').write_text(json.dumps({'metric':metric+1}))"))
    second['depends_on'] = ['a']
    manifest = {'required_inputs': ['refine-logs/EXPERIMENT_PLAN.md'], 'jobs': [first, second]}
    state = WorkflowState('test')
    run(runner, manifest['jobs'], state)
    assert not (tmp_path / 'project-config.json').exists()
    assert json.loads((tmp_path / 'experiment-queue-state.json').read_text())['status'] == 'completed'
    assert json.loads((tmp_path / 'results/b.json').read_text())['metric'] == 2
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 2

    run(runner, manifest['jobs'], state)
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 2
    # Changing a prerequisite invalidates both its receipt and its dependent receipt.
    manifest['jobs'][0] = first | {'argv': [*first['argv'][:-1],
                                         first['argv'][-1].replace('"metric":1', '"metric":3')]}
    run(runner, manifest['jobs'], state)
    assert json.loads((tmp_path / 'results/b.json').read_text())['metric'] == 4
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 4

    (tmp_path / 'results/a.json').write_text('{"metric":NaN}')
    run(runner, manifest['jobs'], state)
    assert json.loads((tmp_path / 'results/b.json').read_text())['metric'] == 4
    assert len(list((tmp_path / 'experiment-runs').glob('*/status.json'))) == 6


def test_block_reassignment_reuses_completed_jobs_and_refreshes_tracker(tmp_path, monkeypatch):
    r = setup_queue(tmp_path, monkeypatch)
    plan = tmp_path / 'refine-logs/EXPERIMENT_PLAN.md'
    plan.write_text('### 区块 B1：原块\n### 区块 B2：新块\n### 区块 D1：未部署\n')
    jobs = [job('a')]
    run(r, jobs)
    jobs[0]['block_id'] = 'B2'
    r.experiments.execute = lambda *a, **k: pytest.fail('metadata change retrained a job')
    run(r, jobs)
    text = (tmp_path / 'refine-logs/EXPERIMENT_TRACKER.md').read_text()
    assert '| B1 | 原块 | 必须执行 | 尚未部署 | 0/0 |' in text
    assert '| B2 | 新块 | 必须执行 | 执行完成 | 1/1 |' in text
    assert '| D1 | 未部署 | 必须执行 | 尚未部署 | 0/0 |' in text
    snapshot = json.loads((tmp_path / 'experiment-queue-state.json').read_text())
    assert snapshot['jobs'][0]['block_id'] == 'B2'


@pytest.mark.parametrize("include_old_receipt", [False, True])
def test_queue_uses_current_receipts_and_bridge_progress_only(tmp_path, monkeypatch, include_old_receipt):
    runner = setup_queue(tmp_path, monkeypatch)
    jobs = [job('a', device='cpu')]
    state = WorkflowState('test')
    if include_old_receipt:
        output = tmp_path / 'results/a.json'
        output.parent.mkdir()
        output.write_text('{"metric":1}')
        state.node_results['experiment_run'] = {'jobs': [{'job': jobs[0], 'status': 'completed'}]}
    events = []
    calls = []
    execute = runner.experiments.execute
    def record(job, **kwargs):
        calls.append(job['id'])
        return execute(job, **kwargs)
    runner.experiments.execute = record
    runner.on_node_progress = lambda phase, state, details: events.append(phase)
    run(runner, jobs, state)
    assert calls == ['a']
    assert len(state.experiment_completed_receipts) == 1
    assert set(events) == {'experiment_bridge'}
    if not include_old_receipt:
        assert state.node_results == {}
