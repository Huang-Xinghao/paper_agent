"""Project evidence reaches agents only after an explicit read tool call."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.workflow import WorkflowRunner, WorkflowState
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.storage import write_json


def test_main_large_read_request_continues_without_losing_text(tmp_path):
    text = '研究证据\n' * 6500
    (tmp_path / 'report.md').write_text(text)
    agent = object.__new__(CoordinatorAgent)
    agent.project = tmp_path
    agent._read_ledger = {}
    first = json.loads(agent._read(None, 'report.md', limit=30000).output)
    assert len(first['content']) == 20000
    second = json.loads(agent._read(None, 'report.md', offset=first['next_offset'], limit=30000).output)
    assert first['content'] + second['content'] == text
    assert second['next_offset'] is None


def test_main_can_recover_from_edit_validation_error(tmp_path):
    import asyncio
    from paper_agent.tools import ToolSpec, ToolResult
    agent = object.__new__(CoordinatorAgent)
    agent.project = tmp_path
    agent.skill_dirs = []
    agent.knowledge_roots = []
    agent._invalid_batch = False
    agent.halted = None
    agent.worker_status = 'completed'
    agent.worker_error = None

    def edit(ctx):
        raise ValueError('Read the current file before editing')

    with pytest.raises(ValueError, match='Read the current file'):
        asyncio.run(agent._invoke(ToolSpec('edit_project_markdown', 'Edit', edit), {}))
    assert agent.halted is None
    assert agent.worker_status == 'completed'
    result = asyncio.run(agent._invoke(ToolSpec('edit_project_markdown', 'Edit',
                                               lambda ctx: ToolResult('Updated')), {}))
    assert result.output == 'Updated'


def test_main_inspects_interrupted_call_without_permanent_dispatch_halt(tmp_path):
    from paper_agent.agent.runtime.sessions import RoleSession
    session = RoleSession(tmp_path, 'main')
    data = session.load()
    data['messages'] = [{'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'interrupted-read', 'type': 'function', 'function': {
            'name': 'get_project_context', 'arguments': '{}'}}]}]
    session.save(data)

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            assert agent.halted is None
            if self.calls == 1:
                assert '上次中断' in messages[0]['content']
                assert any('Execution interrupted' in m.get('content', '') for m in messages)
                return ModelResponse(tool_calls=(ToolCall('inspect', 'get_project_context', {}),))
            return ModelResponse(content='已检查项目当前状态，可按现有授权继续。')

    agent = CoordinatorAgent(tmp_path, model_client=Model(), project_goal='Inspect and resume')
    assert agent.go('Inspect and resume').status == 'completed'
    assert agent.model.calls == 2


@pytest.mark.parametrize('phase,input_path,output_path', [
    ('data_search', 'literature/RESEARCH_REPORT.md', 'data-search/DATA_REPORT.md'),
    ('research_refine', 'idea-stage/IDEA_REPORT.md', 'refine-logs/FINAL_PROPOSAL.md'),
    ('experiment_plan', 'refine-logs/FINAL_PROPOSAL.md', 'refine-logs/EXPERIMENT_PLAN.md'),
])
def test_stage_reads_its_own_inputs(tmp_path, phase, input_path, output_path):
    source = tmp_path / input_path
    source.parent.mkdir(parents=True)
    source.write_text('Private evidence sentinel: split by cell.')
    write_json(tmp_path / 'project-config.json', {'compute': {'cpu_threads': 137}})
    (tmp_path / 'experiments/private-template-sentinel').mkdir(parents=True)
    frozen = tmp_path / 'refine-logs/approved/proposal.md'
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(source.read_bytes())

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                context = json.dumps(messages, ensure_ascii=False)
                assert 'Private evidence sentinel' not in context
                assert '137' not in messages[0]['content']
                assert 'private-template-sentinel' not in context
                assert input_path in context
                return ModelResponse(tool_calls=(ToolCall('input', 'read', {'path': input_path}),))
            if self.calls == 2:
                assert messages[-1]['role'] == 'tool'
                assert not messages[-1].get('is_error')
                assert 'Private evidence sentinel' in messages[-1]['content']
                return ModelResponse(tool_calls=(ToolCall('output', 'write', {
                    'path': output_path, 'content': 'Plan based on the explicitly read evidence.',
                }),))
            return ModelResponse(content='已读取输入并完成交付。')

    agent = PhaseAgent(tmp_path, model_client=Model(), phase=phase)
    runner = WorkflowRunner(agent)
    state = WorkflowState('explicit-read', mode='expert', history=[{
        'event': 'expert_approved', 'node': 'refine',
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'frozen_report': str(frozen.relative_to(tmp_path)),
    }])
    handler = {'data_search': runner._run_data_search, 'research_refine': runner._run_refine,
               'experiment_plan': runner._run_experiment_plan}[phase]
    assert handler(state, 'Complete the assigned stage').artifact == output_path
    assert agent.model.calls == 3


def test_main_gets_paths_then_reads_real_evidence(tmp_path, monkeypatch):
    tex = tmp_path / 'paper/main.tex'
    tex.parent.mkdir()
    tex.write_text(r'\caption{Private caption sentinel}')
    write_json(tmp_path / 'project-config.json', {'compute': {'cpu_threads': 137}})

    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            context = json.dumps(messages, ensure_ascii=False)
            if self.calls == 1:
                assert 'Private caption sentinel' not in context
                assert 'main.tex' not in context
                assert '137' not in messages[0]['content']
                return ModelResponse(tool_calls=(ToolCall('status', 'get_project_context', {}),))
            if self.calls == 2:
                assert 'paper/main.tex' in messages[-1]['content']
                assert 'Private caption sentinel' not in context
                return ModelResponse(tool_calls=(ToolCall('source', 'read_project_file', {'path': 'paper/main.tex'}),))
            assert 'Private caption sentinel' in messages[-1]['content']
            return ModelResponse(content='图注已核对。')

    agent = CoordinatorAgent(tmp_path, model_client=Model(), project_goal='Inspect the figure')
    def forbid_scan(*args, **kwargs): raise AssertionError('No startup project scan')
    original = agent._context
    monkeypatch.setattr(agent, '_context', forbid_scan)
    # The explicit tool retains its bound handler; startup must not call _context.
    assert agent.go('Inspect the figure').status == 'completed'
    assert agent.model.calls == 3
    monkeypatch.setattr(agent, '_context', original)
    assert agent._artifact_handoff(['paper/main.tex']) == [{
        'path': 'paper/main.tex', 'bytes': tex.stat().st_size,
        'sha256': hashlib.sha256(tex.read_bytes()).hexdigest(),
    }]


@pytest.mark.parametrize('mutation', ['unapproved', 'proposal', 'frozen'])
def test_path_only_experiment_plan_preserves_approval_guards(tmp_path, mutation):
    path = tmp_path / 'refine-logs/FINAL_PROPOSAL.md'
    path.parent.mkdir()
    path.write_text('Approved method')
    frozen = tmp_path / 'refine-logs/approved/proposal.md'
    frozen.parent.mkdir()
    frozen.write_bytes(path.read_bytes())
    calls = []
    class Agent:
        project = tmp_path
        def go(self, prompt, **kwargs):
            calls.append(prompt)
            assert 'Approved method' not in prompt
            (path if mutation == 'proposal' else frozen).write_text('Modified method')
            return SimpleNamespace(iterations=1)
    runner = WorkflowRunner(Agent())
    state = WorkflowState('approval', mode='expert', history=[{
        'event': 'expert_approved', 'node': 'refine',
        'sha256': 'unapproved' if mutation == 'unapproved' else hashlib.sha256(path.read_bytes()).hexdigest(),
        'frozen_report': str(frozen.relative_to(tmp_path)),
    }])
    with pytest.raises(ValueError, match='尚未批准|改写'):
        runner._run_experiment_plan(state, 'Plan experiments')
    assert len(calls) == (0 if mutation == 'unapproved' else 1)
