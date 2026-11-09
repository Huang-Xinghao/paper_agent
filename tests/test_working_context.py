import copy
import json

import pytest

from paper_agent.agent.context import WorkingContext, project_messages, size
from paper_agent.agent.loop import AgentState
from paper_agent.agent.model_client import ModelResponse


def exchange(identifier, name='read', output='evidence'):
    return [
        {'role': 'assistant', 'tool_calls': [{'id': identifier, 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps({'path': 'report.md', 'content': 'body' * 1500})}}]},
        {'role': 'tool', 'tool_call_id': identifier, 'name': name, 'content': output},
    ]


def test_projection_elides_duplicates_and_writes_without_mutating_evidence():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
    messages += exchange('write', 'write') + exchange('old', output='evidence' * 1000)
    for i in range(5):
        messages += exchange(str(i))
    messages += exchange('new', output='evidence' * 1000)
    original = copy.deepcopy(messages)
    projected = project_messages(messages)
    assert size(projected) < size(messages)
    assert 'archived_body' in projected[2]['tool_calls'][0]['function']['arguments']
    assert 'duplicate_of_tool_call' in projected[5]['content']
    assert projected[-1]['content'] == 'evidence' * 1000
    assert messages == original


def test_failed_reads_are_never_deduplicated():
    messages = exchange('a', output='error' * 1000)
    messages[-1]['is_error'] = True
    messages += exchange('b', output='error' * 1000) * 6
    assert project_messages(messages)[1]['content'] == 'error' * 1000


def test_mid_task_summary_preserves_prompt_tool_pairs_and_raw_validation_history():
    messages = [{'role': 'system', 'content': 'Skill content unchanged'},
                {'role': 'user', 'content': 'Current assignment: preserve restriction X'}]
    for i in range(12):
        messages += exchange(str(i), output='source evidence ' * 500)
    state = AgentState(messages, 'literature')
    original = copy.deepcopy(state.messages)

    class Model:
        def complete(self, messages, tools):
            assert tools == []
            return ModelResponse(content='Verified report.md; restriction X; unresolved identity.')

    events = []
    context = WorkingContext()
    projected = context.prepare(state, Model(), 10000, lambda: events.append(True))
    assert events == [True]
    assert projected[0] == messages[0]
    assert any(m.get('content') == messages[1]['content'] for m in projected)
    pending = set()
    for m in projected:
        pending.update(c['id'] for c in m.get('tool_calls', []))
        if m['role'] == 'tool':
            assert m['tool_call_id'] in pending
            pending.remove(m['tool_call_id'])
    assert not pending
    assert state.messages == original
    assert len(state.task_messages) == len(messages) - 1
    assert size(projected) < size(messages)


def test_invalid_summary_does_not_change_transcript_or_memory():
    state = AgentState([{'role': 'system', 'content': 'system'},
                        {'role': 'user', 'content': 'task'}] +
                       sum([exchange(str(i), output='large' * 1000) for i in range(8)], []), 'ideas')
    original = copy.deepcopy(state.messages)
    class Model:
        def complete(self, messages, tools):
            return ModelResponse()
    context = WorkingContext()
    with pytest.raises(RuntimeError, match='summary invalid'):
        context.prepare(state, Model(), 1000, lambda: None)
    assert state.messages == original and context.summary is None


@pytest.mark.parametrize('role', ['main', 'idea_generator'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_mid_task_memory_survives_restart_without_resummarizing(tmp_path, role, interrupted):
    from paper_agent.agent.loop import PhaseAgent
    from paper_agent.agent.coordinator import CoordinatorAgent
    from paper_agent.agent.model_client import ToolCall
    from paper_agent.agent.role_sessions import RoleSession
    from paper_agent.tools import ToolResult, ToolSpec

    class Model:
        summaries = 0
        turns = 0
        def complete(self, messages, tools):
            if not tools:
                self.summaries += 1
                assert 'unique covered evidence' in json.dumps(messages)
                return ModelResponse(content='Verified report.md; preserve constraint X; unresolved hypothesis.')
            self.turns += 1
            if self.turns <= 5:
                return ModelResponse(tool_calls=(ToolCall(str(self.turns), 'probe', {}),))
            persisted = RoleSession(tmp_path, role).load()
            assert persisted['context_memory']['summary']
            if interrupted:
                raise RuntimeError('simulated model interruption')
            return ModelResponse(content='Completed evidence review.')

    outputs = iter(['unique covered evidence ' * 1000] + ['small evidence'] * 4)
    model = Model()
    if role == 'main':
        agent = CoordinatorAgent(tmp_path, model_client=model, context_max_chars=6000)
        execute = lambda agent: agent.go('Preserve constraint X')
    else:
        agent = PhaseAgent(tmp_path, model_client=model, context_max_chars=6000)
        execute = lambda agent: agent.go('Preserve constraint X', phase='literature', completion_check=lambda: None)
        # Configure once so the test can supply a deterministic tool.
        agent.configure(phase='literature')
        agent.configure = lambda **kwargs: None
    agent.tools = (ToolSpec('probe', 'Inspect evidence', lambda ctx: ToolResult(next(outputs)), True),)
    agent._by_name = {spec.name: spec for spec in agent.tools}
    if interrupted:
        with pytest.raises(RuntimeError, match='simulated'):
            execute(agent)
    else:
        execute(agent)
    assert model.summaries == 1
    session = RoleSession(tmp_path, role)
    raw = session.load()
    assert 'unique covered evidence' in json.dumps(raw['messages'])
    assert raw['context_memory']['prefix_sha256']

    class NextModel:
        def complete(self, messages, tools):
            assert tools, 'The already-covered prefix must not be summarized again'
            text = json.dumps(messages)
            assert 'Verified report.md' in text
            assert 'Preserve constraint X' in text
            assert 'unique covered evidence' not in text
            return ModelResponse(content='Continue using verified evidence.')
    following = (CoordinatorAgent(tmp_path, model_client=NextModel(), context_max_chars=6000)
                 if role == 'main' else PhaseAgent(tmp_path, model_client=NextModel(), context_max_chars=6000))
    execute(following)
    archives = list((session.folder / 'archives' / role).glob('*.json'))
    assert any(json.loads(path.read_text()) == raw['messages'] for path in archives)
    assert not session.load().get('context_memory')


def test_saved_memory_cannot_cover_changed_evidence(tmp_path):
    from paper_agent.agent.role_sessions import RoleSession
    from paper_agent.agent.context import resume_memory
    state = AgentState([{'role': 'system', 'content': 'system'},
                        {'role': 'user', 'content': 'task'}] +
                       sum([exchange(str(i), output=str(i) * 4000) for i in range(8)], []), 'ideas')
    class Model:
        def complete(self, messages, tools):
            return ModelResponse(content='Verified summary')
    WorkingContext().prepare(state, Model(), 5000, lambda: None)
    raw = copy.deepcopy(state.messages[1:])
    raw[2]['content'] = 'changed evidence'
    assert resume_memory(raw, state.context_memory) is raw
    # A failed replacement summary leaves both raw history and memory intact.
    session = RoleSession(tmp_path, 'idea_generator')
    with session.locked():
        data = session.load()
        data.update(messages=raw, context_memory=state.context_memory)
        session.save(data)
        before = session.path.read_bytes()
        class FailedModel:
            def complete(self, messages, tools):
                return ModelResponse()
        with pytest.raises(RuntimeError, match='summarization failed'):
            session.compact(data, raw, FailedModel(), 1000)
        assert session.path.read_bytes() == before


def test_large_new_tail_summarizes_memory_and_new_evidence_only(tmp_path):
    from paper_agent.agent.role_sessions import RoleSession
    state = AgentState([{'role': 'system', 'content': 'system'},
                        {'role': 'user', 'content': 'keep user constraint'}] +
                       sum([exchange(str(i), output=f'covered-{i} ' * 600) for i in range(8)], []), 'ideas')
    class InitialModel:
        def complete(self, messages, tools):
            return ModelResponse(content='Prior verified memory')
    WorkingContext().prepare(state, InitialModel(), 5000, lambda: None)
    history = state.messages[1:] + exchange('new', output='new evidence ' * 1000)
    session = RoleSession(tmp_path, 'idea_generator')
    class FollowupModel:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            text = json.dumps(messages)
            assert 'Prior verified memory' in text
            assert 'new evidence' in text
            assert 'covered-0' not in text
            return ModelResponse(content='Updated verified memory')
    model = FollowupModel()
    with session.locked():
        data = session.load()
        data.update(messages=history, context_memory=state.context_memory)
        session.save(data)
        compacted = session.compact(data, history, model, 5000)
    assert model.calls == 1
    assert 'Updated verified memory' in compacted[0]['content']
