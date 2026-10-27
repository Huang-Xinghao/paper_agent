import json
import sys
from collections import deque

import pytest

from paper_agent.agent.loop import PhaseAgent, ArtifactValidationError
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState


def setup_project(root):
    (root / 'refine-logs').mkdir()
    for name in ('FINAL_PROPOSAL', 'EXPERIMENT_PLAN', 'EXPERIMENT_TRACKER'):
        (root / f'refine-logs/{name}.md').write_text('### 区块 B1：train and paired statistics\n- 优先级：必须执行' )
    (root / 'experiments').mkdir()
    (root / 'data.csv').write_text('real fixture input')
    (root / 'experiments/job.py').write_text('''import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'mae':0.5}))
if sys.argv[2]=='train':
 c=pathlib.Path('results/train_count.txt')
 c.write_text(str(int(c.read_text())+1) if c.exists() else '1')
''')
    def job(name, path):
        return dict(id=name, block_id='B1', argv=[sys.executable, 'experiments/job.py', path, name],
                    resources={'device': 'cpu'}, depends_on=[], timeout_seconds=5,
                    expected_outputs=[{'path': path, 'metrics': ['mae']}])
    manifest = dict(backend='local', required_inputs=['data.csv'],
                    jobs=[job('train','results/train.json')])
    coverage = [dict(id='B1-train',plan_ref='B1 training',required=True, job_ids=['train'])]
    manifest['requirements'] = coverage
    (root / 'experiments/manifest.json').write_text(json.dumps(manifest))
    return manifest, coverage, job


def call(tool_name, **args):
    return ModelResponse(tool_calls=(ToolCall(tool_name, tool_name, args),))


def write(path, value):
    return call('write', path=path, content=value if isinstance(value,str) else json.dumps(value))


class ScriptedModel:
    def __init__(self, steps): self.steps = deque(steps)
    def complete(self, messages, tools):
        step = self.steps.popleft()
        return step(messages,tools) if callable(step) else step


def runner_for(root, steps):
    model = ScriptedModel(steps)
    agent = PhaseAgent(root, model_client=model, max_iterations=30)
    runner = WorkflowRunner(agent)
    state = WorkflowState('test', current_node='experiment_bridge')
    return runner, agent, state


def test_bridge_runs_then_adds_postprocessing_without_retraining(tmp_path):
    manifest, coverage, job = setup_project(tmp_path)
    post = job('paired_statistics','results/paired.json'); post['depends_on']=['train']
    expanded = {**manifest,'jobs':manifest['jobs']+[post]}
    expanded_coverage = coverage + [dict(id='B1-stats',plan_ref='B1 paired statistics',required=True,
        job_ids=['paired_statistics'])]
    expanded['requirements'] = expanded_coverage
    steps = [call('skill',name='run-experiment'), call('run_experiments'),
             write('experiments/manifest.json',expanded),
             call('run_experiments'),
             write('refine-logs/EXPERIMENT_RESULTS.md','B1 training and statistics complete; negative result'),
             ModelResponse(content='Done')]
    runner,agent,state=runner_for(tmp_path,steps)
    events=[]
    runner.on_node_progress=lambda node,state,detail: events.append((node,detail['stage']))
    result=runner.run('Research',state=state,stop_after='experiment_bridge')
    assert result.status=='completed',result.error
    assert (tmp_path/'results/train_count.txt').read_text()=='1'
    assert len(result.experiment_completed_receipts)==2
    assert [r['node'] for r in result.history]==['experiment_bridge']
    assert runner._next_node(result,'experiment_bridge')=='paper_plan'
    assert result.artifacts['experiment_bridge']=='refine-logs/EXPERIMENT_RESULTS.md'
    assert events and all(node=='experiment_bridge' for node,_ in events)
    agent.configure(phase='paper_plan')
    assert 'run_experiments' not in agent._by_name
    assert 'check_experiments' not in agent._by_name




def test_missing_data_pauses_at_bridge_without_running_jobs(tmp_path):
    setup_project(tmp_path);(tmp_path/'data.csv').unlink()
    runner,agent,state=runner_for(tmp_path,[call('run_experiments'),ModelResponse(content='Missing input')])
    result=runner.run('Research',state=state)
    assert result.status=='awaiting_results' and result.current_node=='experiment_bridge'
    assert not list((tmp_path/'experiment-runs').glob('*/status.json'))


def test_bridge_repairs_failed_job_inside_same_phase(tmp_path):
    _,coverage,_=setup_project(tmp_path)
    good=(tmp_path/'experiments/job.py').read_text()
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    steps=[call('run_experiments'),write('experiments/job.py',good),
           call('run_experiments'),
           
           write('refine-logs/EXPERIMENT_RESULTS.md','Real results'),ModelResponse(content='Done')]
    runner,agent,state=runner_for(tmp_path,steps)
    result=runner.run('Research',state=state,stop_after='experiment_bridge')
    assert result.status=='completed',result.error
    assert result.experiment_repair_count==1 and result.experiment_feedback is None
    receipts=[json.loads(p.read_text()) for p in (tmp_path/'experiment-runs').glob('*/status.json')]
    assert sum(r['status']=='failed' for r in receipts)==1
    assert [r['node'] for r in result.history]==['experiment_bridge']




def test_consumed_repair_guard_does_not_survive_model_failure(tmp_path):
    setup_project(tmp_path)

    def fail_model(messages, tools):
        raise RuntimeError('provider disconnected')

    runner, agent, state = runner_for(tmp_path, [
        call('run_experiments'), fail_model])
    state.experiment_feedback = {'error': 'previous failure',
                                'protected_evidence': runner._experiment_evidence_snapshot()}
    result = runner.run('Research', state=state, stop_after='experiment_bridge')
    assert result.status == 'failed' and 'provider disconnected' in result.error
    assert 'protected_evidence' not in result.experiment_feedback
    assert (tmp_path/'results/train.json').exists()


@pytest.mark.parametrize('path,change', [
    ('results/metric.json', 'modified'),
    ('results/metric.json', 'removed'),
    ('results/metric.json', 'added'),
    ('project-config.json', 'modified'),
])
def test_repair_guard_reports_changed_evidence(tmp_path, path, change):
    setup_project(tmp_path)
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if change != 'added':
        target.write_text('{}')
    runner, _, state = runner_for(tmp_path, [call('run_experiments'), ModelResponse(content='Done')])
    state.experiment_feedback = {'protected_evidence': runner._experiment_evidence_snapshot()}
    if change == 'removed':
        target.unlink()
    else:
        target.write_text('{ }\n' if path.endswith('config.json')
                          else '{"changed": true}')
    result = runner.run('Research', state=state, stop_after='experiment_bridge')
    assert result.status == 'failed'
    assert f'{path} ({change})' in result.error
    assert not (tmp_path / 'experiment-runs').exists()


def test_source_repair_with_output_symlink_preserves_evidence(tmp_path):
    setup_project(tmp_path)
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/retained.json').write_text('{"mae": 0.5}')
    (tmp_path / 'experiments/outputs').symlink_to('../results', target_is_directory=True)
    good = (tmp_path / 'experiments/job.py').read_text()
    runner, _, state = runner_for(tmp_path, [
        write('experiments/job.py', good), call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md', 'Measured result'), ModelResponse(content='Done')])
    (tmp_path / 'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    state.experiment_feedback = {'protected_evidence': runner._experiment_evidence_snapshot()}
    result = runner.run('Research', state=state, stop_after='experiment_bridge')
    assert result.status == 'completed', result.error
    assert (tmp_path / 'results/retained.json').read_text() == '{"mae": 0.5}'






def test_restart_reuses_previous_completed_jobs(tmp_path):
    _,coverage,_=setup_project(tmp_path)
    steps=[call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md','Measured result'),ModelResponse(content='Done')]
    runner,agent,state=runner_for(tmp_path,steps)
    runner.run('Research',state=state,stop_after='experiment_bridge')
    saved=WorkflowState.from_dict(state.to_dict())
    second,_,_=runner_for(tmp_path,[call('run_experiments'),ModelResponse(content='Done')])
    second.start_from_project('experiment_bridge','Research',saved)
    result=second.run('Research',state=saved,stop_after='experiment_bridge')
    assert result.status=='completed',result.error
    assert (tmp_path/'results/train_count.txt').read_text()=='1'


def test_missing_input_remains_waiting_when_model_rounds_exhausted(tmp_path):
    setup_project(tmp_path);(tmp_path/'data.csv').unlink()
    runner,agent,state=runner_for(tmp_path,[call('run_experiments'),call('run_experiments')])
    agent.max_iterations=2
    result=runner.run('Research',state=state)
    assert result.status=='awaiting_results' and 'Missing experiment inputs' in result.error


