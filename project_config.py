"""User-owned compute policy shared by planning and execution."""
import json
import math
from pathlib import Path

MAX_JOB_SECONDS = 86400


def load_compute_config(project):
    project = Path(project)
    path = project / 'project-config.json'
    if path.exists():
        document = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or set(document) - {'compute'}:
            raise ValueError('project-config.json supports only the compute object')
        config = document.get('compute', {})
        allowed = {'device', 'gpu_index', 'cpu_threads', 'max_job_seconds', 'total_experiment_seconds'}
    else:
        path = project / 'experiments/resources.json'
        config = json.loads(path.read_text()) if path.exists() else {}
        allowed = {'device', 'gpu_index', 'cpu_threads'}
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
            '\nExecution limits: ' + json.dumps({'effective_max_job_seconds': effective_job_seconds(project),
                                                    'runner_max_job_seconds': MAX_JOB_SECONDS}) +
            '\nnull means no project-specific limit, not missing approval; the runner limit still applies. '
            'Split long searches by seed/variant into separate executable jobs, each within the effective limit. '
            'Choose and record reasonable job budgets within configured limits. '
            'Use these resources to size models, seeds and searches. Total budget includes '
            'smoke tests and all experiment attempts across resumes; it excludes model API time. '
            'Inspect real device availability before execution; auto is a preference, not proof of a GPU.\n')
