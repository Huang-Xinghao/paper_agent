import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.project_config import load_compute_config, compute_context
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState, WorkflowConfig, NodeResult, AwaitingResults
from paper_agent.tools import ToolContext, invoke
from paper_agent.agent.experiment_queue import run_queue


def configure(project, **compute):
    (project / 'project-config.json').write_text(json.dumps({'compute': compute}))


def runner(project):
    r = WorkflowRunner(SimpleNamespace(project=project))
    r.experiments.available_devices = lambda: {'cpu': True, 'cuda_count': 0, 'mps': False}
    return r


def job(seconds=1, code=None):
    return {'id': 'test', 'depends_on': [], 'argv': [sys.executable, '-c', code or
        "from pathlib import Path; Path('results').mkdir(exist_ok=True); Path('results/x.json').write_text('{\"x\":1}')"],
        'timeout_seconds': seconds, 'expected_outputs': [{'path': 'results/x.json', 'metrics': ['x']}]}


def test_only_project_config_controls_compute(tmp_path):
    (tmp_path / 'experiments').mkdir()
    (tmp_path / 'experiments/resources.json').write_text('{"device":"cpu","cpu_threads":2}')
    assert load_compute_config(tmp_path)['cpu_threads'] is None
    configure(tmp_path, device='auto', max_job_seconds=10)
    assert load_compute_config(tmp_path)['cpu_threads'] is None
    assert load_compute_config(tmp_path)['max_job_seconds'] == 10


@pytest.mark.parametrize('config', [dict(max_job_seconds=True), dict(total_experiment_seconds=-1),
    dict(max_job_seconds=float('nan')), dict(cpu_threads=0), dict(device='cpu',gpu_index=0), dict(memory_gb=1)])
def test_invalid_config(tmp_path, config):
    configure(tmp_path, **config)
    with pytest.raises(ValueError):
        load_compute_config(tmp_path)


def test_runner_passes_original_task_without_duplicate_policy(tmp_path):
    configure(tmp_path, cpu_threads=3, total_experiment_seconds=42)
    r=runner(tmp_path)
    captured=[]
    r._nodes['custom'] = lambda state, prompt: (captured.append(prompt) or NodeResult())
    state=r.run('original task', state=WorkflowState('test', current_node='custom'))
    assert captured == ['original task']
    assert state.task_prompt == 'original task'


def test_agent_cannot_change_user_policy(tmp_path):
    configure(tmp_path, device='cpu')
    ctx=ToolContext(tmp_path)
    result=asyncio.run(invoke('write',ctx,path='project-config.json',content='{}'))
    assert result.is_error
    assert load_compute_config(tmp_path)['device']=='cpu'


def test_per_job_rejection_before_launch(tmp_path):
    configure(tmp_path, max_job_seconds=1)
    with pytest.raises(ValueError,match='max_job_seconds'):
        runner(tmp_path).experiments.execute(job(2))
    assert not (tmp_path/'experiment-runs').exists()


def test_cumulative_budget_survives_restart_and_counts_failures(tmp_path):
    configure(tmp_path, device='cpu', total_experiment_seconds=0.05)
    with pytest.raises(RuntimeError):
        runner(tmp_path).experiments.execute(job(5, 'import time; time.sleep(5)'))
    receipt=json.loads(next((tmp_path/'experiment-runs').glob('*/status.json')).read_text())
    assert receipt['status']=='failed'
    assert receipt['effective_timeout_seconds'] <= 0.05
    assert receipt['elapsed_seconds'] >= 0.05
    with pytest.raises(AwaitingResults,match='exhausted'):
        runner(tmp_path).experiments.execute(job())


def test_receipt_and_remaining_budget(tmp_path):
    configure(tmp_path, device='cpu', cpu_threads=2, max_job_seconds=2,total_experiment_seconds=10)
    receipt=runner(tmp_path).experiments.execute(job())
    assert receipt['status']=='completed'
    assert receipt['resources']['requested']['total_experiment_seconds']==10
    assert runner(tmp_path).experiments.seconds_used()==receipt['elapsed_seconds']


def test_interrupted_receipt_does_not_reset_budget(tmp_path):
    configure(tmp_path, total_experiment_seconds=10)
    p=tmp_path/'experiment-runs/interrupted';p.mkdir(parents=True)
    (p/'status.json').write_text('{"status":"running"}')
    with pytest.raises(AwaitingResults,match='Unfinished'):
        runner(tmp_path).experiments.execute(job())


def test_manifest_over_budget_rejected_before_execution(tmp_path):
    configure(tmp_path, max_job_seconds=1)
    (tmp_path/'experiments').mkdir()
    manifest={'backend':'local','required_inputs':['experiments/manifest.json'], 'jobs':[job(2)]}
    (tmp_path/'experiments/manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='max_job_seconds'):
        runner(tmp_path)._experiment_manifest()


def test_direct_phase_system_prompt_has_policy(tmp_path):
    from paper_agent.agent.loop import PhaseAgent
    configure(tmp_path, device='cpu', cpu_threads=7)
    agent=PhaseAgent(tmp_path, model_client=SimpleNamespace())
    assert '"cpu_threads": 7' in agent._system_prompt()
    assert 'do not edit or override' in agent._system_prompt()


def test_runner_limit_is_in_prompt_even_without_user_limit(tmp_path):
    from paper_agent.project_config import validate_job_timeout
    assert '"effective_max_job_seconds": 86400' in compute_context(tmp_path)
    configure(tmp_path, max_job_seconds=500000)
    assert '"effective_max_job_seconds": 86400' in compute_context(tmp_path)
    with pytest.raises(ValueError, match='test.*432000.*86400'):
        validate_job_timeout(tmp_path, job(432000))
    configure(tmp_path, max_job_seconds=600)
    assert '"effective_max_job_seconds": 600' in compute_context(tmp_path)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, 0, '3600'])
def test_invalid_timeout_rejected_in_direct_executor(tmp_path, value):
    with pytest.raises(ValueError, match='timeout_seconds'):
        runner(tmp_path).experiments.execute(job(value))
    assert not (tmp_path/'experiment-runs').exists()


def test_unresolved_requirement_does_not_block_execution(tmp_path):
    (tmp_path / 'refine-logs').mkdir()
    (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Training')
    r=runner(tmp_path)
    manifest={'required_inputs':['refine-logs/EXPERIMENT_PLAN.md'], 'jobs':[job() | {'block_id': 'B1'}], 'requirements':[
        {'id':'training', 'plan_ref':'Main training', 'required':True,
         'job_ids':[], 'reason':'No future labels'}]}
    r.experiments.execute=lambda value, **kwargs: {'job':value, 'status':'completed', 'log':'actual.log'}
    state=WorkflowState('test')
    r.experiments.validate_inputs(manifest)
    run_queue(r.experiments, state, manifest)
    assert state.experiment_completed_receipts[0]['status']=='completed'
