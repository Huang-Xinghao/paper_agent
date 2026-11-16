"""Agent-visible template installation, isolation and editable standalone copies."""

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from paper_agent.tools import builtin_tools, invoke, tools_for_phase
from paper_agent.tools.core import ToolContext
from paper_agent.tools.experiment_template import prepare_experiment_template, prepare_template_run


def unpack(result):
    assert not result.is_error, result.output
    return json.loads(result.output)


@pytest.mark.parametrize('domain,tasks,dataset,model', [
    ('cell', ['life'], 'Tongji', 'IC2ML'),
    ('cell', ['soh', 'trajectory', 'rul'], 'HUST', 'IC2ML'),
    ('electrolyte', ['conductivity'], 'bamboo_mixer', 'CompositionMLP'),
])
def test_tool_copies_complete_editable_template(tmp_path, domain, tasks, dataset, model):
    data = tmp_path / 'source-data'
    data.mkdir()
    sample = data / 'sample.json'
    sample.write_text('source data is not copied')
    project = tmp_path / 'project'
    project.mkdir()
    result = asyncio.run(invoke('prepare_experiment_template', ToolContext(project),
                               domain=domain, tasks=tasks, dataset=dataset,
                               data_path=str(data)))
    output = unpack(result)
    assert output['destination'] == 'experiments/template'
    destination = project / output['destination']
    config = json.loads((destination / 'task_config.json').read_text())
    assert config['tasks'] == tasks and config['dataset'] == dataset
    assert config['domain'] == domain and config['model'] == model
    assert config['root_path'] == str(data)
    assert output['train_argv'] == ['python', 'run.py', '--config', 'task_config.json', '--stage', 'train']
    assert output['project_train_argv'][-2:] == ['--stage', 'train']
    assert output['project_test_argv'][-2:] == ['--stage', 'test']
    assert output['metric_fields'] == [f'{task}.mae' for task in tasks]
    assert output['cwd'] == output['destination']
    assert output['project_train_argv'][1] == output['destination'] + '/run.py'
    for entry in output['entry_points']:
        assert (project / entry).is_file()
    receipt = json.loads((destination / 'template_manifest.json').read_text())
    assert receipt['template'] == 'template'
    assert receipt['template_sha256'] == output['template_sha256']
    assert output['file_count'] == len(list(p for p in destination.rglob('*') if p.is_file()))
    assert (destination / 'data_provider/LICENSE.BatteryLife').is_file()
    assert (destination / 'tests/test_universal.py').is_file()
    for relative, digest in receipt['files'].items():
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
    assert not any(p.name in {'.git', '__pycache__', 'checkpoint', 'sample.json'}
                   for p in destination.rglob('*'))
    assert sample.read_text() == 'source data is not copied'
    (destination / 'run.py').write_text('# edited by agent\n')
    other = unpack(prepare_experiment_template(ToolContext(project), domain,
                                               destination='experiments/another'))
    assert (project / other['destination'] / 'run.py').read_text() != '# edited by agent\n'
    retry = prepare_experiment_template(ToolContext(project), domain)
    assert retry.is_error and retry.metadata['error_code'] == 'FileExistsError'
    assert (destination / 'run.py').read_text() == '# edited by agent\n'


def test_defaults_and_workspace_relative_data(tmp_path):
    ctx = ToolContext(tmp_path)
    first = unpack(prepare_experiment_template(ctx, 'cell'))
    assert first['tasks'] == ['life'] and first['requires_data_path']
    assert first['data_path'] == './dataset'
    second = unpack(prepare_experiment_template(ctx, 'electrolyte', data_path='inputs/formulas.json',
                                               destination='experiments/electrolyte'))
    assert second['data_path'] == str(tmp_path / 'inputs/formulas.json')
    assert not second['requires_data_path']


@pytest.mark.parametrize('kwargs', [
    {'domain': 'unknown'}, {'domain': 'cell', 'tasks': []},
    {'domain': 'cell', 'tasks': ['conductivity']},
    {'domain': 'electrolyte', 'tasks': ['rul']},
    {'domain': 'cell', 'tasks': 'life'},
    {'domain': 'cell', 'dataset': 'not-a-dataset'},
    {'domain': 'electrolyte', 'dataset': 'Tongji'},
    {'domain': 'cell', 'destination': '../escape'},
    {'domain': 'cell', 'destination': '/tmp/outside-template'},
    {'domain': 'cell', 'destination': '.'},
    {'domain': 'cell', 'destination': ''},
])
def test_invalid_requests_do_not_install_files(tmp_path, kwargs):
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    result = prepare_experiment_template(ToolContext(tmp_path), **kwargs)
    assert result.is_error
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before


def test_symlink_escape_and_existing_code_are_preserved(tmp_path):
    project, external = tmp_path / 'project', tmp_path / 'outside'
    project.mkdir()
    external.mkdir()
    (project / 'linked').symlink_to(external, target_is_directory=True)
    ctx = ToolContext(project)
    assert prepare_experiment_template(ctx, 'cell', destination='linked/code').is_error
    assert not list(external.iterdir())
    (project / 'experiments').mkdir()
    (project / 'experiments/template').write_text('existing file')
    assert prepare_experiment_template(ctx, 'cell').is_error
    assert (project / 'experiments/template').read_text() == 'existing file'


def test_template_tool_visible_to_experiment_bridge_only(tmp_path):
    from paper_agent.agent.coordinator import CoordinatorAgent
    from paper_agent.agent.loop import tool_schema
    main = CoordinatorAgent(tmp_path, model_client=object(), project_goal='Research')
    assert 'prepare_experiment_template' not in {spec.name for spec in main.tools}
    assert 'prepare_experiment_template' in {
        spec.name for spec in tools_for_phase('experiment_bridge', builtin_tools())}
    from paper_agent.tools import PHASE_TOOL_NAMES
    for phase in set(PHASE_TOOL_NAMES) - {'experiment_bridge'}:
        assert 'prepare_experiment_template' not in {
            spec.name for spec in tools_for_phase(phase, builtin_tools())}
    spec = next(spec for spec in builtin_tools() if spec.name == 'prepare_experiment_template')
    assert not spec.read_only
    schema = tool_schema(spec)['function']['parameters']
    assert schema['required'] == ['domain']
    assert schema['properties']['domain']['enum'] == ['cell', 'electrolyte']
    assert schema['properties']['tasks']['type'] == 'array'


def test_failed_copy_removes_staging_and_preserves_existing_project(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'existing.py').write_text('keep me')
    original = Path.write_bytes

    def fail_during_copy(path, data):
        if path.name == 'run.py':
            raise OSError('simulated disk write failure')
        return original(path, data)

    monkeypatch.setattr(Path, 'write_bytes', fail_during_copy)
    result = prepare_experiment_template(ToolContext(project), 'cell')
    assert result.is_error
    assert not (project / 'experiments/template').exists()
    assert not list(project.rglob('.prediction-template-*'))
    assert (project / 'existing.py').read_text() == 'keep me'


def test_installed_python_files_compile_without_source_checkout(tmp_path):
    result = unpack(prepare_experiment_template(ToolContext(tmp_path), 'electrolyte'))
    destination = Path(result['absolute_path'])
    # Compilation needs no ML dependencies or imports from the original source tree.
    for path in destination.rglob('*.py'):
        compile(path.read_text(), str(path), 'exec')
    check = subprocess.run([sys.executable, '-c',
                            'from pathlib import Path; import json; '
                            'assert Path("run.py").is_file(); '
                            'assert json.load(open("task_config.json"))["domain"] == "electrolyte"'],
                           cwd=destination, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_template_runs_snapshot_config_and_release_tests_after_training(tmp_path):
    ctx = ToolContext(tmp_path)
    unpack(prepare_experiment_template(ctx, 'cell'))
    config_path = tmp_path / 'experiments/template/task_config.json'
    config = json.loads(config_path.read_text())
    first = unpack(prepare_template_run(ctx, 'seed1'))
    assert len(first['jobs']) == 2 and not first['test_released']
    assert json.loads((tmp_path / first['configuration']).read_text()) == config
    unpack(prepare_template_run(ctx, 'seed1', include_test=True))
    config['seed'] += 1
    config_path.write_text(json.dumps(config))
    second = unpack(prepare_template_run(ctx, 'seed2', include_test=True))
    manifest = json.loads((tmp_path / 'experiments/manifest.json').read_text())
    assert len(manifest['jobs']) == 6
    trains = {first['run_id'] + '_train', second['run_id'] + '_train'}
    for job in manifest['jobs']:
        if job['id'].endswith('_test'):
            assert set(job['depends_on']) == trains
        elif job['id'].endswith('_train'):
            assert job['depends_on'] == [job['id'].removesuffix('_train') + '_check']
    before = (tmp_path / 'experiments/manifest.json').read_bytes()
    unpack(prepare_template_run(ctx, 'seed2', include_test=True))
    assert (tmp_path / 'experiments/manifest.json').read_bytes() == before
    assert json.loads((tmp_path / first['configuration']).read_text())['seed'] != config['seed']


def test_new_experiment_tools_are_scoped_to_experiment_bridge():
    from paper_agent.tools import PHASE_TOOL_NAMES
    names = {'prepare_template_run', 'experiment_resources'}
    for phase in PHASE_TOOL_NAMES:
        visible = {spec.name for spec in tools_for_phase(phase, builtin_tools())}
        assert names & visible == (names if phase == 'experiment_bridge' else set())


def test_installed_template_executes_generated_jobs_through_real_queue(tmp_path):
    from types import SimpleNamespace
    from paper_agent.tools.experiment_resources import experiment_resources
    from paper_agent.tools.experiments.executor import ExperimentExecutor
    from paper_agent.tools.experiments.queue import run_queue

    data = tmp_path / 'electrolyte.json'
    data.write_text(json.dumps([
        dict(solvents=[dict(smiles='CCO', molar_ratio=1.0)],
             salts=[dict(smiles='[Li+].[Cl-]', molar_ratio=1.0)],
             salt_molar_ratio=0.01 * (group + 1), temperature=25.0,
             conductivity=float(group + 1), conductivity_mask=True)
        for group in range(12)]))
    ctx = ToolContext(tmp_path)
    unpack(prepare_experiment_template(ctx, 'electrolyte', data_path=str(data)))
    config_path = tmp_path / 'experiments/template/task_config.json'
    config = json.loads(config_path.read_text())
    config.update(hidden_dim=8, fingerprint_dim=8, batch_size=4,
                  train_epochs=1, patience=1, num_workers=0)
    config_path.write_text(json.dumps(config))
    experiment_resources(ctx, 'configure', device='cpu', cpu_threads=1)
    (tmp_path / 'refine-logs').mkdir()
    (tmp_path / 'refine-logs/EXPERIMENT_PLAN.md').write_text('## B1: Template smoke\n')
    result = unpack(prepare_template_run(ctx, 'smoke', device='cpu', include_test=True))
    executor = ExperimentExecutor(tmp_path)
    manifest = executor.validate_manifest(json.loads((tmp_path / result['manifest']).read_text()))
    executor.validate_inputs(manifest)
    receipts = run_queue(executor, SimpleNamespace(experiment_completed_receipts=[]), manifest, track=False)
    assert len(receipts) == 3 and all(r['status'] == 'completed' for r in receipts)
    assert all(r['compute_backend']['device'] == 'cpu' for r in receipts)
    root = tmp_path / result['outputs']
    assert not (root / 'train/metrics.json').exists()
    assert not (root / 'test/checkpoint.pth').exists()
    assert json.loads((root / 'check/preflight.json').read_text())['test_scored'] is False
    assert json.loads((root / 'test/metrics.json').read_text())['conductivity']['observed_values'] > 0
