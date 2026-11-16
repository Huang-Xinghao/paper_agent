"""Install editable prediction code into a project without running experiments."""

from __future__ import annotations

import hashlib
import fcntl
import json
from pathlib import Path, PurePosixPath
import shutil
import re
import tempfile
from typing import Literal
from zipfile import ZipFile

from .core import ToolContext, ToolResult, tool

BUNDLE = Path(__file__).resolve().parents[1] / 'templates' / 'template.zip'
TASKS = {'cell': ('life', 'soh', 'trajectory', 'rul'),
         'electrolyte': ('conductivity', 'anion_ratio')}
DEFAULT_TASKS = {'cell': ('life',), 'electrolyte': ('conductivity',)}
CELL_DATASETS = {'NCA', 'NCM', 'NCMNCA'}  # Other names come from the bundled split catalog.


@tool()
def prepare_experiment_template(
    ctx: ToolContext,
    domain: Literal['cell', 'electrolyte'],
    tasks: list[str] | None = None,
    dataset: str | None = None,
    data_path: str | None = None,
    destination: str = 'experiments/template',
) -> ToolResult:
    """Copy editable prediction code and a selected task_config.json into the workspace.

    Call before implementing an experiment when no suitable project code exists.
    cell tasks: life (default), soh, trajectory, rul; dataset defaults to Tongji and
    uses BatteryLife fixed cell splits. electrolyte tasks: conductivity (default),
    anion_ratio; dataset=bamboo_mixer. Its CompositionMLP is a baseline, not the
    Bamboo-mixer architecture. Missing observed labels fail when data is loaded.
    data_path references a dataset root or electrolyte JSON; discover/inspect it
    with battery_resources first. Omit it to leave ./dataset for later editing.
    destination must be a NEW workspace subdirectory; existing code is never
    overwritten. Returns cwd, editable entry points, argv and template fingerprint.
    Copies code/config/tests only; does not copy data/checkpoints, install packages
    or start training. Afterwards edit the copied code/config and use the existing
    experiment runner for training. Reuse existing project code on retries.
    """
    staging = None
    try:
        if domain not in TASKS:
            raise ValueError(f'Unknown domain: {domain}')
        if tasks is not None and (not isinstance(tasks, list) or not tasks
                                  or any(not isinstance(task, str) for task in tasks)):
            raise ValueError('tasks must be a nonempty list of task names')
        selected = list(dict.fromkeys(DEFAULT_TASKS[domain] if tasks is None else tasks))
        if set(selected) - set(TASKS[domain]):
            raise ValueError(f'{domain} supports tasks {TASKS[domain]}')
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError('destination must name a new workspace subdirectory')
        requested = Path(destination)
        if requested.is_absolute() or '..' in requested.parts:
            raise ValueError('destination must be relative to the workspace without ..')
        # Reject symlink parents even if the link happens to point inside workspace.
        cursor = ctx.workspace.resolve()
        for part in requested.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('destination cannot contain symlinks')
        target = ctx.resolve(destination, write=True)
        if target == ctx.workspace.resolve() or target.exists():
            raise FileExistsError('Destination already exists; reuse its code or choose a new directory')
        if data_path is not None and (not isinstance(data_path, str) or not data_path.strip()):
            raise ValueError('data_path must be a nonempty dataset path')
        if dataset is not None and (not isinstance(dataset, str) or not dataset):
            raise ValueError('dataset must be a nonempty name')
        dataset = dataset or ('Tongji' if domain == 'cell' else 'bamboo_mixer')
        with ZipFile(BUNDLE) as archive:
            if domain == 'cell':
                catalog = json.loads(archive.read('data_provider/batterylife_splits.json'))['splits']
                if dataset not in set(catalog) | CELL_DATASETS:
                    raise ValueError(f'Unknown BatteryLife dataset {dataset!r}')
            elif dataset != 'bamboo_mixer':
                raise ValueError('Electrolyte template supports dataset=bamboo_mixer')
            preset = ('cell_multitask.json' if domain == 'cell' else 'electrolyte_conductivity.json')
            config = json.loads(archive.read(f'configs/{preset}'))
            config.update(domain=domain, tasks=selected, dataset=dataset)
            # Explicit relative paths refer to the agent workspace, not the copied cwd.
            config['root_path'] = str((ctx.workspace / data_path).resolve()) if data_path else './dataset'
            files = {}
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in info.filename
                        or any(part.startswith('.') for part in path.parts)
                        or info.is_dir() or info.filename in files):
                    raise ValueError(f'Invalid bundled template entry: {info.filename}')
                files[info.filename] = archive.read(info)
        # Publish a completed directory; failed copies leave no half-installed template.
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.prediction-template-', dir=target.parent))
        for relative, content in files.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / 'task_config.json').write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
        fingerprint = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
        receipt = {'template': 'template', 'template_sha256': fingerprint,
                   'domain': domain, 'tasks': selected, 'dataset': dataset,
                   'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
                   'configuration': 'task_config.json'}
        (staging / 'template_manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')
        # Directory rename refuses an existing populated destination. Also check
        # immediately before publishing to preserve files created during copying.
        if target.exists() or target.is_symlink():
            raise FileExistsError('Destination appeared during preparation; no existing code was changed')
        staging.rename(target)
        staging = None
        relative = target.relative_to(ctx.workspace.resolve()).as_posix()
        result = {'destination': relative, 'absolute_path': str(target), 'cwd': relative,
                  'domain': domain, 'tasks': selected, 'dataset': dataset,
                  'model': config['model'], 'data_path': config['root_path'],
                  'config': f'{relative}/task_config.json',
                  'entry_points': [f'{relative}/{name}' for name in
                                   ('run.py', 'exp/experiment.py', f'adapters/{domain}.py',
                                    'models/registry.py')],
                  'train_argv': ['python', 'run.py', '--config', 'task_config.json', '--stage', 'train'],
                  'project_train_argv': ['python', f'{relative}/run.py', '--config',
                                         f'{relative}/task_config.json', '--stage', 'train'],
                  'project_test_argv': ['python', f'{relative}/run.py', '--config',
                                        f'{relative}/task_config.json', '--stage', 'test'],
                  'project_check_argv': ['python', f'{relative}/run.py', '--config',
                                         f'{relative}/task_config.json', '--stage', 'check'],
                  'queue_tool': 'prepare_template_run', 'resource_tool': 'experiment_resources',
                  'outputs': {'directory_option': '--output_dir',
                              'validation_metrics': 'validation_metrics.json',
                              'test_metrics': 'metrics.json',
                              'targets': 'targets.csv', 'predictions': 'predictions.csv',
                              'configuration': 'data_manifest.json'},
                  'metric_fields': [f'{task}.mae' for task in selected],
                  'file_count': len(files) + 2, 'template_sha256': fingerprint,
                  'requires_data_path': data_path is None,
                  'next_step': 'Read task_config.json and adapter/model code; implement the approved method. '
                               'Use experiment_resources to inspect/configure the agreed compute policy and '
                               'prepare_template_run to create standard check/train jobs for run_experiments. '
                               'train_argv uses the returned cwd; project_train_argv runs from the project root '
                               'and can be adapted into experiments/manifest.json jobs for the existing runner. '
                               'Set --output_dir per job. train saves validation results; test restores the checkpoint. '
                               'Read task-qualified metric fields such as life.mae. '
                               'Use the plan to select evaluation windows, statistical units and resources.'}
        return ToolResult(json.dumps(result, ensure_ascii=False, indent=2),
                          metadata={'destination': relative, 'domain': domain}, structured_content=result)
    except (ValueError, OSError, KeyError) as error:
        return ToolResult(str(error), True, {'error_code': type(error).__name__})
    finally:
        if staging is not None:
            shutil.rmtree(staging)


@tool()
def prepare_template_run(ctx: ToolContext, name: str,
                         config_path: str = 'experiments/template/task_config.json',
                         template_path: str = 'experiments/template', block_id: str = 'B1',
                         device: Literal['auto', 'cpu', 'cuda', 'mps'] = 'auto',
                         include_test: bool = False, timeout_seconds: float = 3600) -> ToolResult:
    """Add standard template jobs to experiments/manifest.json without running them.

    Edit the model and task configuration, then call this tool for each planned
    model/seed. It snapshots configuration and creates check -> train jobs with
    separate output directories. run_experiments handles scheduling, receipts,
    failure costs and resume; no extra receipt/runner script is needed.
    After completing model/configuration selection, call again with include_test
    to append checkpoint-restoring test jobs. Test depends on every currently
    declared template training job; prepare all training runs before releasing tests.
    The check stage verifies the production loader/adapter/loss, gradients and
    disk restore without training or reporting test scores. Research-specific
    comparisons and analysis remain editable project code.
    """
    from paper_agent.project_config import effective_job_seconds
    from paper_agent.storage import write_json
    from .experiments.executor import ExperimentExecutor
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('name must be a simple run identifier')
    if device not in {'auto', 'cpu', 'cuda', 'mps'} or type(include_test) is not bool:
        raise ValueError('Choose device auto/cpu/cuda/mps and boolean include_test')
    template = ctx.resolve(template_path)
    config_file = ctx.resolve(config_path)
    entry = (template / 'run.py').relative_to(ctx.workspace.resolve()).as_posix()
    if not (template / 'run.py').is_file():
        raise ValueError('Prepare the template before creating a run')
    config = json.loads(config_file.read_text())
    tasks = config.get('tasks')
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or not t for t in tasks):
        raise ValueError('Configuration must select tasks explicitly')
    if config.get('itr', 1) != 1:
        raise ValueError('Create one named run per seed with itr=1')
    encoded = json.dumps(config, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:12]
    identifier = f'{name}_{digest}'
    snapshot = f'experiments/configs/{identifier}.json'
    root = f'results/runs/{identifier}'
    timeout = min(timeout_seconds, effective_job_seconds(ctx.workspace))
    metrics = [f'{task}.mae' for task in tasks]
    jobs = []
    for stage in ('check', 'train', 'test'):
        directory = f'{root}/{stage}'
        argv = ['python', entry, '--config', snapshot, '--stage', stage,
                '--output_dir', directory]
        if stage == 'test':
            argv += ['--checkpoint', f'{root}/train']
        outputs = ([{'path': f'{directory}/preflight.json', 'nonempty': True}] if stage == 'check' else
                   [{'path': f'{directory}/validation_metrics.json', 'metrics': metrics},
                    {'path': f'{directory}/checkpoint.pth'},
                    {'path': f'{directory}/data_manifest.json', 'nonempty': True}] if stage == 'train' else
                   [{'path': f'{directory}/metrics.json', 'metrics': metrics},
                    {'path': f'{directory}/predictions.csv', 'nonempty': True},
                    {'path': f'{directory}/targets.csv', 'nonempty': True}])
        jobs.append({'id': f'{identifier}_{stage}', 'block_id': block_id, 'argv': argv,
                     'resources': {'device': device}, 'timeout_seconds': timeout,
                     'depends_on': [] if stage == 'check' else [f'{identifier}_' +
                                                              ('check' if stage == 'train' else 'train')],
                     'expected_outputs': outputs})
    executor = ExperimentExecutor(ctx.workspace)
    directory = ctx.workspace / '.paper-agent'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'experiment-executor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Experiment queue is running; prepare additional runs after it returns')
        manifest_path = ctx.resolve('experiments/manifest.json', write=True)
        manifest = (json.loads(manifest_path.read_text()) if manifest_path.exists() else
                    {'backend': 'local', 'required_inputs': [], 'jobs': []})
        current = {job['id']: job for job in manifest['jobs']}
        selected = jobs if include_test else jobs[:2]
        for job in selected:
            if job['id'] in current and current[job['id']] != job:
                # Previously released tests may already depend on other training jobs.
                prior = dict(current[job['id']], depends_on=job['depends_on'])
                if prior != job:
                    raise ValueError(f'Run {job["id"]} already has a different execution definition; choose another name')
            elif job['id'] not in current:
                manifest['jobs'].append(job)
        # Keep released tests behind the complete declared template training set.
        train_ids = [job['id'] for job in manifest['jobs']
                     if job.get('argv', [None, None])[1] == entry and '--stage' in job['argv']
                     and job['argv'][job['argv'].index('--stage') + 1] == 'train']
        for job in manifest['jobs']:
            if job.get('argv', [None, None])[1] == entry and '--stage' in job['argv']:
                if job['argv'][job['argv'].index('--stage') + 1] == 'test':
                    job['depends_on'] = list(dict.fromkeys(job.get('depends_on', []) + train_ids))
        manifest['required_inputs'] = list(dict.fromkeys(manifest.get('required_inputs', []) + [entry, snapshot]))
        executor.validate_manifest(manifest)
        destination = ctx.resolve(snapshot, write=True)
        if destination.exists() and json.loads(destination.read_text()) != config:
            raise ValueError('Run configuration snapshot was changed; choose a new run name')
        write_json(destination, config)
        write_json(manifest_path, manifest)
    result = {'manifest': 'experiments/manifest.json', 'run_id': identifier,
              'configuration': snapshot, 'jobs': [job['id'] for job in selected],
              'outputs': root, 'test_released': include_test,
              'next_step': 'Call run_experiments; inspect its receipts and task-qualified metrics.'}
    return ToolResult(json.dumps(result, ensure_ascii=False), structured_content=result)
