import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent.workflow import WorkflowRunner


@pytest.mark.parametrize('report', [None, {}, {
    'device': 'cpu', 'framework': 'numpy',
    'evidence': 'CPU array implementation; no GPU operations',
}])
def test_cuda_allocation_does_not_imply_cuda_execution(tmp_path, monkeypatch, report):
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    monkeypatch.setattr(runner.experiments, 'available_devices',
                        lambda: {'cpu': True, 'cuda_count': 1, 'mps': False})
    script = ('import json,os,pathlib; '
              'p=pathlib.Path("results/metric.json"); p.parent.mkdir(exist_ok=True); '
              'p.write_text(json.dumps({"metric":1})); ')
    if report is not None:
        script += ('pathlib.Path(os.environ["PAPER_AGENT_BACKEND_REPORT"]).write_text('
                   + repr(json.dumps(report)) + ')')
    job = {'id': 'backend', 'argv': [sys.executable, '-c', script],
           'timeout_seconds': 5, 'expected_outputs': [{'path': 'results/metric.json', 'metrics': ['metric']}]}
    receipt = runner.experiments.execute(job)
    assert receipt['resources']['allocated_device'] == 'cuda'
    assert 'device' not in receipt['resources']
    backend = receipt['compute_backend']
    if report:
        assert backend['status'] == 'reported'
        assert backend['device'] == 'cpu'
        assert backend['source'] == 'experiment_process'
    else:
        assert backend['status'] == 'unknown'
        assert 'device' not in backend
    saved = json.loads(next((tmp_path / 'experiment-runs').glob('*/status.json')).read_text())
    assert saved['compute_backend'] == backend
