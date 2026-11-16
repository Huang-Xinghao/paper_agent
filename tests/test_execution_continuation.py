"""Recover format and worker boundary failures without replaying completed work."""
import json
from types import SimpleNamespace

import pytest

from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.tools.wiki_review import request_json_analysis


@pytest.mark.parametrize('content', [
    '```json\n{"passed": false}\n```',
    'Review follows:\n{"passed": false}\nEnd of review.',
])
def test_json_format_wrappers_preserve_rejection(content):
    model = SimpleNamespace(complete=lambda messages, tools: ModelResponse(content=content))
    assert request_json_analysis(model, 'Review', 'Evidence') == {'passed': False}


def test_json_repair_preserves_inputs_and_does_not_run_tools():
    calls = []
    responses = iter(['{"passed": false,}', '{"passed": false}'])
    def complete(messages, tools):
        calls.append((messages, tools))
        return ModelResponse(content=next(responses))
    assert request_json_analysis(SimpleNamespace(complete=complete), 'Review', 'Evidence') == {'passed': False}
    assert len(calls) == 2
    assert calls[1][0][:2] == calls[0][0]
    assert calls[1][0][2]['content'] == '{"passed": false,}'
    assert all(tools == [] for _, tools in calls)


def test_json_format_failure_is_bounded():
    calls = []
    def complete(messages, tools):
        calls.append(messages)
        return ModelResponse(content='not JSON')
    with pytest.raises(ValueError, match='after 3 format attempts'):
        request_json_analysis(SimpleNamespace(complete=complete), 'Review', 'Evidence')
    assert len(calls) == 3
    assert [len(messages) for messages in calls] == [2, 4, 4]


def test_role_continues_past_segment_and_completion_correction_without_tool_replay(tmp_path):
    from paper_agent.agent.loop import AgentLoop, PhaseAgent, ArtifactValidationError
    from paper_agent.agent.coordinator import CoordinatorAgent
    from paper_agent.tools.core import ToolSpec, ToolResult
    assert PhaseAgent.auto_continue_iterations and CoordinatorAgent.auto_continue_iterations
    events, saved, writes, requests = [], [], [], []
    responses = iter([
        ModelResponse(tool_calls=[ToolCall(id='write-1', name='write_once', arguments={})]),
        ModelResponse(content='Done?'),
        ModelResponse(content='Done.'),
    ])
    def complete(messages, tools):
        requests.append(list(messages))
        return next(responses)
    def write_once(ctx):
        writes.append(True)
        (ctx.workspace / 'report.md').write_text('Saved evidence')
        return ToolResult('Saved report.md')
    class RoleLoop(AgentLoop):
        auto_continue_iterations = True
        def _save_conversation(self, state):
            saved.append(json.loads(json.dumps(state.messages)))
    loop = RoleLoop(SimpleNamespace(complete=complete), workspace=tmp_path,
        tools=(ToolSpec('write_once', 'Write report', write_once),), max_iterations=1,
        connect_mcp=False, on_progress=lambda kind, data: events.append(kind))
    checks = []
    def check():
        checks.append(True)
        if len(checks) == 1:
            raise ArtifactValidationError('Explain the saved report in your final response')
    state = loop.run('Write report', system_prompt='Complete task', phase='test', completion_check=check)
    assert state.iterations == 3 and state.final == 'Done.'
    assert len(writes) == 1 and len(checks) == 2
    assert events.count('execution_continued') == 2
    assert any(message.get('tool_call_id') == 'write-1' for message in requests[-1])
    assert any('Completion checks failed' in str(message) for message in saved[-1])
    checkpoint = json.loads((tmp_path / '.paper-agent/phase-progress/test.json').read_text())
    assert checkpoint['status'] == 'phase_completed' and checkpoint['iteration'] == 3


@pytest.mark.parametrize('backend', ['local', 'herdr'])
def test_lost_worker_result_recovers_and_allows_next_dispatch(tmp_path, backend):
    import fcntl
    from paper_agent.agent.runtime.execution import ExecutionTask, recover_orphaned_task
    from paper_agent.agent.runtime.herdr import directory, ensure_no_pending, wait, HerdrError
    from paper_agent.storage import write_json
    task = ExecutionTask(id='b' * 32, project=str(tmp_path), node='wiki', kind='wiki',
                         role='wiki_agent', instruction='Archive paper', project_goal='Research')
    root = directory(tmp_path)
    folder = root / 'tasks' / task.id
    folder.mkdir(parents=True)
    write_json(folder / 'request.json', task.model_dump())
    write_json(folder / 'started.json', {'id': task.id, 'backend': backend,
                                       'pane_id': 'pane-1', 'pid': 123})
    write_json(root / 'active.json', {'id': task.id, 'consumed': False})
    inbox = root / 'roles' / task.role / 'inbox.json'
    write_json(inbox, {'id': task.id})
    with (folder / 'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(HerdrError, match='still executing'):
            ensure_no_pending(tmp_path)
        assert not (folder / 'result.json').exists() and inbox.exists()
    result = wait(tmp_path, task.id, wait_seconds=.1)
    assert result.status == 'interrupted'
    assert result.execution['backend'] == backend and result.execution['pane_id'] == 'pane-1'
    assert not inbox.exists() and (folder / 'cancel').exists()
    receipt = (folder / 'result.json').read_bytes()
    assert not recover_orphaned_task(folder)
    assert (folder / 'result.json').read_bytes() == receipt
    ensure_no_pending(tmp_path)
    assert json.loads((root / 'active.json').read_text())['consumed'] is True


def test_wiki_definite_interruption_does_not_latch_main_dispatch(tmp_path, monkeypatch):
    from paper_agent.agent import coordinator
    (tmp_path / 'paper.pdf').write_bytes(b'%PDF-test')
    main = coordinator.CoordinatorAgent(tmp_path, model_client=object(), mode='auto')
    calls = []
    def execute(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status='interrupted' if len(calls) == 1 else 'completed',
                               error='Worker exited' if len(calls) == 1 else None, wiki_result=None)
    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    assert main._curate_wiki(None, paper_path='paper.pdf').is_error
    assert main.halted is None
    assert not main._curate_wiki(None, paper_path='paper.pdf').is_error
    assert len(calls) == 2
