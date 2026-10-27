"""User-owned compute policy shared by planning and execution."""
import json
import math
from pathlib import Path

MAX_JOB_SECONDS = 86400


def load_compute_config(project):
    project = Path(project)
    path = project / 'project-config.json'
    document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(document, dict) or set(document) - {'compute', 'execution'}:
        raise ValueError('project-config.json supports only compute and execution objects')
    config = document.get('compute', {})
    allowed = {'device', 'gpu_index', 'cpu_threads', 'max_job_seconds', 'total_experiment_seconds'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError(f'{path.name} has unsupported compute fields')
    result = {'device': 'auto', 'gpu_index': None, 'cpu_threads': None,
              'max_job_seconds': None, 'total_experiment_seconds': None, **config}
    if result['device'] not in {'auto', 'cpu', 'cuda', 'mps'}:
        raise ValueError('device must be auto, cpu, cuda or mps')
    for name, minimum in [('gpu_index', 0), ('cpu_threads', 1)]:
        value = result[name]
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError(f'{name} must be an integer >= {minimum}')
    if result['gpu_index'] is not None and result['device'] in {'cpu', 'mps'}:
        raise ValueError('gpu_index requires auto or cuda device')
    for name in ('max_job_seconds', 'total_experiment_seconds'):
        value = result[name]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError(f'{name} must be a finite positive number or null')
    return result


def load_execution_config(project):
    path = Path(project) / 'project-config.json'
    document = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(document, dict) or set(document) - {'compute', 'execution'}:
        raise ValueError('project-config.json supports only compute and execution objects')
    config = document.get('execution', {})
    defaults = dict(gpu_ids='auto', max_parallel_gpu_jobs='auto',
                    gpu_idle_memory_threshold_mib=500, oom_max_retries=2,
                    oom_retry_delay_seconds=120, poll_seconds=2)
    if not isinstance(config, dict) or set(config) - set(defaults):
        raise ValueError('unsupported execution configuration')
    result = defaults | config
    ids = result['gpu_ids']
    if ids != 'auto' and (not isinstance(ids, list) or not ids or
                          any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids)):
        raise ValueError('gpu_ids must be auto or distinct nonnegative GPU indices')
    parallel = result['max_parallel_gpu_jobs']
    if parallel != 'auto' and (type(parallel) is not int or parallel < 1):
        raise ValueError('max_parallel_gpu_jobs must be auto or a positive integer')
    for key in ('gpu_idle_memory_threshold_mib', 'oom_max_retries'):
        if type(result[key]) is not int or result[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    for key in ('oom_retry_delay_seconds', 'poll_seconds'):
        if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] <= 0:
            raise ValueError(f'{key} must be finite and positive')
    return result


def effective_job_seconds(project):
    configured = load_compute_config(project)["max_job_seconds"]
    return min(MAX_JOB_SECONDS, configured) if configured is not None else MAX_JOB_SECONDS


def validate_job_timeout(project, job):
    value = job.get("timeout_seconds")
    limit = effective_job_seconds(project)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= limit:
        raise ValueError(
            f"Job {job.get('id', '<missing>')}: timeout_seconds={value!r} must be finite and "
            f"0 < timeout_seconds <= {limit} (effective max_job_seconds). "
            "Split multi-seed/variant searches into executable jobs; do not use the whole experiment budget as one job timeout.")


def compute_context(project):
    return ('User-owned project compute policy (do not edit or override):\n' +
            json.dumps(load_compute_config(project), ensure_ascii=False) +
            '\nUser-owned execution policy:\n' + json.dumps(load_execution_config(project), ensure_ascii=False) +
            '\nEvery manifest job must declare depends_on as a list of upstream job IDs. '
            'Use depends_on: [] for independent jobs; list prerequisite job IDs for dependent jobs. '
            '\nExecution limits: ' + json.dumps({'effective_max_job_seconds': effective_job_seconds(project),
                                                    'runner_max_job_seconds': MAX_JOB_SECONDS}) +
            '\nnull means no project-specific limit, not missing approval; the runner limit still applies. '
            'Split long searches by seed/variant into separate executable jobs, each within the effective limit. '
            'Choose and record reasonable job budgets within configured limits. '
            'Use these resources to size models, seeds and searches. Total budget includes '
            'all experiment attempts across resumes; it excludes model API time. '
            'Inspect real device availability before execution; auto is a preference, not proof of a GPU.\n')
