import json
from types import SimpleNamespace

import pytest

from paper_agent.project_config import load_compute_config, load_execution_config
from paper_agent.tools.core import ToolContext
from paper_agent.tools.experiment_resources import experiment_resources
from paper_agent.tools.experiments import queue
from paper_agent.tools.experiments.executor import ExperimentExecutor


def test_gpu_ids_schema_matches_configure_validation(tmp_path):
    from paper_agent.agent.loop import tool_schema
    from paper_agent.tools import builtin_tools

    spec = next(tool for tool in builtin_tools() if tool.name == 'experiment_resources')
    properties = tool_schema(spec)['function']['parameters']['properties']
    assert properties['gpu_ids'] == {'type': 'array', 'items': {'type': 'integer'}}
    experiment_resources(ToolContext(tmp_path), 'configure', gpu_ids=[0, 1, 2, 3])
    assert load_execution_config(tmp_path)['gpu_ids'] == [0, 1, 2, 3]


def test_configure_translates_and_preserves_resource_policy(tmp_path):
    ctx = ToolContext(tmp_path)
    experiment_resources(ctx, 'configure', device='cuda', gpu_ids=[1, 2],
                         cpu_threads=4, budget_hours=2, max_job_seconds=300,
                         max_parallel_gpu_jobs=2, gpu_idle_memory_threshold_mib=800)
    assert load_compute_config(tmp_path) == dict(device='cuda', gpu_index=None,
        cpu_threads=4, total_experiment_seconds=7200, max_job_seconds=300)
    execution = load_execution_config(tmp_path)
    assert execution['gpu_ids'] == [1, 2] and execution['max_parallel_gpu_jobs'] == 2
    assert execution['gpu_idle_memory_threshold_mib'] == 800
    experiment_resources(ctx, 'configure', cpu_threads=8)
    assert load_compute_config(tmp_path)['total_experiment_seconds'] == 7200
    before = (tmp_path / 'project-config.json').read_bytes()
    with pytest.raises(ValueError):
        experiment_resources(ctx, 'configure', gpu_ids=[1, 1])
    assert (tmp_path / 'project-config.json').read_bytes() == before


def test_inspect_reports_nvml_failure_without_changing_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(ExperimentExecutor, 'available_devices',
                        staticmethod(lambda: dict(cpu=True, cuda_count=1, mps=False)))

    def fail(_executor):
        raise RuntimeError('NVML mismatch')

    monkeypatch.setattr(queue, 'discover_gpus', fail)
    result = experiment_resources(ToolContext(tmp_path))
    assert not result.is_error
    assert result.structured_content['available_devices']['cuda_count'] == 1
    assert result.structured_content['gpu_discovery_error'] == 'NVML mismatch'
    assert not (tmp_path / 'project-config.json').exists()


def test_queue_persists_gpu_wait_reason_and_clears_it_on_completion(tmp_path, monkeypatch):
    state = SimpleNamespace(experiment_completed_receipts=[])
    notices = []
    job = dict(id='one', resources={'device': 'cuda'})
    executor = SimpleNamespace(project=tmp_path,
        resolve_resources=lambda job: dict(allocated_device='cuda'),
        execute=lambda job, **kwargs: dict(job=job, status='completed'))
    calls = 0

    def discover(_executor):
        nonlocal calls
        calls += 1
        return {0: dict(token='test-gpu', memory_used_mib=900 if calls < 3 else 0)}

    def progress(_phase, _state, event):
        if event['stage'] == 'waiting_for_gpu':
            notices.append(json.loads((tmp_path / 'experiment-queue-state.json').read_text()))

    monkeypatch.setattr(queue, 'discover_gpus', discover)
    monkeypatch.setattr(queue, 'read_plan_blocks', lambda project: [])
    monkeypatch.setattr(queue.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(queue.time, 'sleep', lambda seconds: None)
    queue.run_queue(executor, state, {'jobs': [job]}, track=False, on_progress=progress)
    assert notices[0]['waiting']['reason'] == 'waiting_for_gpu'
    assert notices[0]['waiting']['gpu_memory_used_mib'] == {'0': 900}
    final = json.loads((tmp_path / 'experiment-queue-state.json').read_text())
    assert final['status'] == 'completed' and 'waiting' not in final


def test_cpu_resume_reuses_completed_gpu_job_without_nvml(tmp_path, monkeypatch):
    from paper_agent.storage import write_json
    gpu = dict(id='trained', argv=['python', 'train.py'], resources={'device': 'cuda'},
               expected_outputs=[{'path': 'results/model.bin'}], depends_on=[])
    cpu = dict(id='analyze', argv=['python', 'analyze.py'], resources={'device': 'cpu'},
               expected_outputs=[], depends_on=['trained'])
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/model.bin').write_text('Existing checkpoint')
    write_json(tmp_path / 'experiment-runs/old/status.json',
               dict(job=gpu, status='completed', elapsed_seconds=2))
    executor = SimpleNamespace(project=tmp_path, validate_outputs=lambda job: None,
        resolve_resources=lambda job: dict(allocated_device='cpu'),
        execute=lambda job, **kwargs: dict(job=job, status='completed'))
    def fail(_):
        raise AssertionError('CPU-only resume must not query NVML')
    monkeypatch.setattr(queue, 'discover_gpus', fail)
    monkeypatch.setattr(queue, 'read_plan_blocks', lambda _: [])
    monkeypatch.setattr(queue.time, 'sleep', lambda _: None)
    outcomes = queue.run_queue(executor, SimpleNamespace(), {'jobs': [gpu, cpu]}, track=False)
    assert [item['job']['id'] for item in outcomes] == ['trained', 'analyze']
