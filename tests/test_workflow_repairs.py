import json
import sys
from types import SimpleNamespace

import pytest

from paper_agent.agent.workflow import (
    ArtifactValidationError, AwaitingResults, NodeResult,
    WorkflowConfig, WorkflowRunner, WorkflowState,
)


@pytest.mark.parametrize('error', [TimeoutError('network'), AwaitingResults('missing data')])
def test_non_contract_errors_not_repaired(tmp_path, error):
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    calls = []
    def handler(*args):
        calls.append(1)
        raise error
    runner._nodes['ideas'] = handler
    state = runner.run('task', state=WorkflowState('test', current_node='ideas'))
    assert len(calls) == 1 and state.fault_retry_count == 0
    assert state.status == ('awaiting_results' if isinstance(error, AwaitingResults) else 'failed')


from test_bridge_orchestration import setup_project, runner_for, call, write
from paper_agent.agent.model_client import ModelResponse


@pytest.mark.parametrize('limit', [0, 1, 3])
def test_experiment_repair_budget_stops_repeated_failed_jobs(tmp_path, limit):
    setup_project(tmp_path)
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    steps=[call('run_experiments') for _ in range(limit+2)]+[ModelResponse(content='Cannot repair')]
    runner,agent,state=runner_for(tmp_path,steps)
    runner.config=WorkflowConfig(max_experiment_repairs=limit)
    result=runner.run('task',state=state,stop_after='experiment_bridge')
    assert result.status=='failed' and 'repair limit' in result.error
    assert len(list((tmp_path/'experiment-runs').glob('*/status.json')))==limit+1


def test_repair_cannot_tamper_with_failed_receipt(tmp_path):
    setup_project(tmp_path)
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    def tamper(messages,tools):
        path=next((tmp_path/'experiment-runs').glob('*/status.json'))
        return write(str(path.relative_to(tmp_path)),'{}')
    runner,agent,state=runner_for(tmp_path,[call('run_experiments'),tamper,
        call('run_experiments'),ModelResponse(content='Done')])
    result=runner.run('task',state=state,stop_after='experiment_bridge')
    assert result.status=='failed' and 'protected evidence' in result.error


def test_authorization_failure_is_terminal_without_code_repair(tmp_path):
    setup_project(tmp_path)
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('HTTP 403 Forbidden')")
    runner,agent,state=runner_for(tmp_path,[call('run_experiments'),ModelResponse(content='Blocked')])
    result=runner.run('task',state=state)
    assert result.status=='failed' and 'authorization' in result.error
    assert result.experiment_repair_count==0


def test_resource_budget_blocks_repair_rerun(tmp_path):
    setup_project(tmp_path)
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    runner,agent,state=runner_for(tmp_path,[call('run_experiments'),call('run_experiments'),
        ModelResponse(content='Budget exhausted')])
    real_execute=runner.experiments.execute
    calls=[]
    def execute(*args, **kwargs):
        calls.append(1)
        if len(calls)>1: raise AwaitingResults('Project total_experiment_seconds exhausted')
        return real_execute(*args, **kwargs)
    runner.experiments.execute=execute
    result=runner.run('task',state=state)
    assert result.status=='awaiting_results' and result.experiment_repair_count==1
    assert len(list((tmp_path/'experiment-runs').glob('*/status.json')))==1


def test_pending_execution_repair_survives_restart(tmp_path):
    _,coverage,_=setup_project(tmp_path)
    good=(tmp_path/'experiments/job.py').read_text()
    (tmp_path/'experiments/job.py').write_text("raise RuntimeError('broken implementation')")
    runner,agent,state=runner_for(tmp_path,[call('run_experiments')])
    saved=[]
    def interrupt(node,state,details):
        if details['stage']=='repair_required':
            saved.append(state.to_dict())
            raise KeyboardInterrupt()
    runner.on_node_progress=interrupt
    with pytest.raises(KeyboardInterrupt): runner.run('task',state=state)
    resumed=WorkflowState.from_dict(saved[0])
    runner,agent,_=runner_for(tmp_path,[write('experiments/job.py',good),call('run_experiments'),
        call('run_experiments'),
        write('refine-logs/EXPERIMENT_RESULTS.md','Measured result'),ModelResponse(content='Done')])
    runner.start_from_project('experiment_bridge','task',resumed)
    result=runner.run('task',state=resumed,stop_after='experiment_bridge')
    assert result.status=='completed',result.error
    assert result.experiment_repair_count==1 and result.experiment_feedback is None


def test_completion_validation_exhausts_the_single_model_budget(tmp_path):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent.model_client import ModelResponse

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            return ModelResponse(content='Finished without the proposal')

    model = Model()
    agent = PhaseAgent(tmp_path, model_client=model, max_iterations=3)
    runner = WorkflowRunner(agent)
    with pytest.raises(ArtifactValidationError, match='FINAL_PROPOSAL'):
        runner._go('Refine the idea', phase='research_refine')
    assert model.calls == 3
    assert len([m for m in agent.last_state.messages if m['role'] == 'system']) == 1


def test_manifest_validation_is_repaired_in_same_bridge_call(tmp_path):
    manifest,coverage,_=setup_project(tmp_path)
    steps=[write('experiments/manifest.json',{}),ModelResponse(content='Package ready'),
        write('experiments/manifest.json',manifest),call('run_experiments'),
        
        write('refine-logs/EXPERIMENT_RESULTS.md','Measured result'),ModelResponse(content='Done')]
    runner,agent,state=runner_for(tmp_path,steps)
    result=runner.run('task',state=state,stop_after='experiment_bridge')
    assert result.status=='completed',result.error
    assert result.experiment_repair_count==0
    assert any('Completion checks failed' in m.get('content','') for m in agent.last_state.messages
               if isinstance(m.get('content'),str))


def test_latex_validation_uses_same_agent_history(tmp_path, monkeypatch):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent.model_client import ModelResponse, ToolCall

    (tmp_path / 'paper').mkdir()
    source = tmp_path / 'paper/main.tex'
    source.write_text('broken')
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 2:
                assert 'Completion checks failed: bad TeX' in messages[-1]['content']
                return ModelResponse(tool_calls=(ToolCall('fix', 'edit', {
                    'path': 'paper/main.tex', 'old': 'broken', 'new': 'fixed'}),))
            return ModelResponse(content='Compiled')

    agent = PhaseAgent(tmp_path, model_client=Model(), max_iterations=3)
    runner = WorkflowRunner(agent)
    def compile_pdf():
        if source.read_text() != 'fixed':
            raise RuntimeError('bad TeX')
        pdf = tmp_path / 'paper/main.pdf'
        pdf.write_bytes(b'%PDF-1.4' + b'0' * 100)
        return pdf
    monkeypatch.setattr(runner, '_compile_pdf', compile_pdf)
    result = runner._run_paper_compile(WorkflowState('test'), 'Compile')
    assert result.artifact == 'paper/main.pdf'
    assert agent.last_state.iterations == 3
