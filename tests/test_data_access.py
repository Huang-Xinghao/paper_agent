import asyncio
import json
import pickle
from types import SimpleNamespace

import pytest

from paper_agent.tools import ToolContext, invoke, tools_for_phase, BUILTIN_TOOLS
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState, AwaitingResults
from paper_agent.agent.experiment_queue import run_queue


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    data = tmp_path / 'external'
    data.mkdir()
    knowledge = tmp_path / 'knowledge'
    (knowledge / '数据集').mkdir(parents=True)
    (knowledge / '数据集/CALB_README.md').write_text(
        f'- 处理后数据路径：`{data}`\nOther text `/etc` does not grant access.\n')
    (data / 'cell.pkl').write_bytes(pickle.dumps({'cell_id': 'cell', 'cycle_data': [
        {'cycle_number': 1, 'voltage_in_V': [3.2, 3.3]}]}))
    (data / 'labels.json').write_text('{"cell": 50}')
    return ToolContext(workspace, knowledge_roots=(knowledge,)), data, knowledge


def call(ctx, name, **kwargs):
    return asyncio.run(invoke(name, ctx, **kwargs))


def test_registered_data_read_and_inspect(setup):
    ctx, data, _ = setup
    assert str(data) in call(ctx, 'list_battery_data_sources').output
    assert str(data / 'cell.pkl') in call(ctx, 'find', path=str(data), pattern='*.pkl').output
    assert 'cell' in call(ctx, 'read', path=str(data / 'labels.json')).output
    result = call(ctx, 'inspect_battery_data', path=str(data / 'cell.pkl'))
    assert not result.is_error, result.output
    assert json.loads(result.output)['summary']['fields']['cell_id'] == 'cell'
    assert not call(ctx, 'grep', path=str(data), pattern='cell').is_error


def test_nested_dataset_library_registration(setup):
    ctx, data, knowledge = setup
    nested = knowledge / '电芯/数据集'
    nested.mkdir(parents=True)
    (knowledge / '数据集/CALB_README.md').rename(nested / 'CALB_README.md')
    sources = json.loads(call(ctx, 'list_battery_data_sources').output)['sources']
    assert sources == [{'resource_id': '电芯/数据集/CALB_README',
                        'kind': '处理后数据路径', 'path': str(data), 'exists': True}]
    assert not call(ctx, 'inspect_battery_data', path=str(data / 'cell.pkl')).is_error
    resources = json.loads(call(ctx, 'search_battery_resources', query='CALB_README',
                                resource_types=['dataset']).output)
    assert resources['count'] == 1
    assert call(ctx, 'write', path=str(data / 'labels.json'), content='bad').is_error


def test_non_dataset_documents_do_not_register_paths(setup):
    ctx, _, knowledge = setup
    (knowledge / 'notes.md').write_text('- 处理后数据路径：`/etc`\n')
    hidden = knowledge / '.hidden/数据集'
    hidden.mkdir(parents=True)
    (hidden / 'secret.md').write_text('- 处理后数据路径：`/etc`\n')
    assert call(ctx, 'read', path='/etc/passwd').is_error


def test_manifest_preserves_access_without_description_fields(setup):
    ctx, data, knowledge = setup
    (knowledge / '数据集/CALB_README.md').write_text('# CALB\n')
    (knowledge / 'data_sources.json').write_text(json.dumps({'sources': [
        {'resource_id': '电芯/数据集/CALB_README', 'kind': '处理后数据路径',
         'path': str(data)}]}))
    assert str(data) in call(ctx, 'list_battery_data_sources').output
    assert not call(ctx, 'inspect_battery_data', path=str(data / 'cell.pkl')).is_error
    assert call(ctx, 'write', path=str(data / 'labels.json'), content='bad').is_error


@pytest.mark.parametrize('path', ['/', 'relative/data'])
def test_manifest_rejects_invalid_roots(setup, path):
    ctx, _, knowledge = setup
    (knowledge / 'data_sources.json').write_text(json.dumps({'sources': [
        {'resource_id': 'invalid', 'kind': '处理后数据路径', 'path': path}]}))
    from paper_agent.tools.data_access import registered_data_sources
    with pytest.raises(ValueError, match='Invalid registered data path'):
        registered_data_sources(ctx.knowledge_roots)


def test_write_and_symlink_escape_rejected(setup, tmp_path):
    ctx, data, _ = setup
    assert call(ctx, 'write', path=str(data / 'labels.json'), content='bad').is_error
    assert call(ctx, 'edit', path=str(data / 'labels.json'), old='50', new='0').is_error
    assert json.loads((data / 'labels.json').read_text()) == {'cell': 50}
    outside = tmp_path / 'secret.json'
    outside.write_text('secret')
    (data / 'escape.json').symlink_to(outside)
    (ctx.workspace / 'data').symlink_to(data, target_is_directory=True)
    assert call(ctx, 'read', path=str(data / 'escape.json')).is_error
    assert call(ctx, 'write', path='data/labels.json', content='bad').is_error
    assert 'escape.json' not in call(ctx, 'find', path=str(data), pattern='*.json').output
    assert 'secret' not in call(ctx, 'grep', path=str(data), pattern='secret').output
    assert call(ctx, 'read', path='/etc/passwd').is_error


def test_experiment_input_and_output_boundaries(setup, tmp_path):
    ctx, data, knowledge = setup
    (ctx.workspace / 'refine-logs').mkdir(exist_ok=True)
    (ctx.workspace / 'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Read registered data')
    runner = WorkflowRunner(SimpleNamespace(project=ctx.workspace, knowledge_root=knowledge))
    assert runner.experiments.input_path(str(data / 'cell.pkl')) == data / 'cell.pkl'
    with pytest.raises(ValueError):
        runner.experiments.input_path(str(tmp_path / 'unknown'))
    with pytest.raises(ValueError):
        runner.experiments.output_path(str(data / 'out.json'))
    # Missing but registered data is a missing-input condition, not an access error.
    assert not runner.experiments.input_path(str(data / 'missing.pkl')).exists()



def test_data_inspection_visible_to_all_required_phases():
    for phase in ('ideas', 'novelty', 'research_refine',
                  'experiment_plan', 'experiment_bridge'):
        assert {'inspect_battery_data', 'list_battery_data_sources', 'read_battery_resource'} <= {
            t.name for t in tools_for_phase(phase, BUILTIN_TOOLS)}


def test_job_reads_registered_external_input_and_writes_workspace(setup, monkeypatch):
    ctx, data, knowledge = setup
    (ctx.workspace / 'experiments').mkdir()
    (ctx.workspace / 'experiments/read_data.py').write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "value=json.loads(Path(sys.argv[1]).read_text())\n"
        "Path('results').mkdir(exist_ok=True)\n"
        "Path('results/read.json').write_text(json.dumps({'value':value['cell']}))\n")
    job = {'id': 'formal', 'block_id': 'B1', 'depends_on': [], 'argv': ['python', 'experiments/read_data.py', str(data / 'labels.json')],
           'timeout_seconds': 10, 'resources': {'device': 'cpu'},
           'expected_outputs': [{'path': 'results/read.json', 'metrics': ['value']}]}
    manifest = {'backend': 'local', 'required_inputs': [str(data / 'labels.json')], 'jobs': [job]}
    (ctx.workspace / 'experiments/manifest.json').write_text(json.dumps(manifest))
    (ctx.workspace / 'refine-logs').mkdir(exist_ok=True)
    (ctx.workspace / 'refine-logs/EXPERIMENT_PLAN.md').write_text('### 区块 B1：Read registered data')
    runner = WorkflowRunner(SimpleNamespace(project=ctx.workspace, knowledge_root=knowledge))
    monkeypatch.setattr(runner.experiments, 'available_devices', lambda: {'cpu': True, 'cuda_count': 0, 'mps': False})
    manifest = runner.experiments.load_manifest()
    runner.experiments.validate_inputs(manifest)
    receipts = run_queue(runner.experiments, WorkflowState('test'), manifest)
    assert receipts[0]['status'] == 'completed'
    assert json.loads((ctx.workspace / 'results/read.json').read_text()) == {'value': 50}
    assert json.loads((data / 'labels.json').read_text()) == {'cell': 50}
