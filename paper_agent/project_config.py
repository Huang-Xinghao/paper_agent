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
    return validate_compute_config(document.get('compute', {}))


def validate_compute_config(config):
    allowed = {'device', 'gpu_index', 'cpu_threads', 'max_job_seconds', 'total_experiment_seconds'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('project-config.json has unsupported compute fields')
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
    return validate_execution_config(document.get('execution', {}))


def validate_execution_config(config):
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


def update_resources(project, *, device=None, gpu_ids=None, cpu_threads=None,
                     budget_hours=None, max_job_seconds=None,
                     max_parallel_gpu_jobs=None, gpu_idle_memory_threshold_mib=None):
    """Translate resource requests and atomically publish a validated configuration."""
    import fcntl
    from paper_agent.storage import write_json
    project = Path(project).resolve()
    directory = project / '.paper-agent'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'resource-config.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        compute = load_compute_config(project)
        execution = load_execution_config(project)
        for key, value in {'device': device, 'cpu_threads': cpu_threads,
                           'max_job_seconds': max_job_seconds}.items():
            if value is not None:
                compute[key] = value
        if budget_hours is not None:
            if type(budget_hours) not in (int, float) or not math.isfinite(budget_hours) or budget_hours <= 0:
                raise ValueError('budget_hours must be finite and positive')
            compute['total_experiment_seconds'] = budget_hours * 3600
        if gpu_ids is not None:
            execution['gpu_ids'] = gpu_ids
            compute['gpu_index'] = None  # Queue allocates a logical device from gpu_ids.
        for key, value in {'max_parallel_gpu_jobs': max_parallel_gpu_jobs,
                           'gpu_idle_memory_threshold_mib': gpu_idle_memory_threshold_mib}.items():
            if value is not None:
                execution[key] = value
        config = {'compute': validate_compute_config(compute),
                  'execution': validate_execution_config(execution)}
        write_json(project / 'project-config.json', config)
        return config


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
