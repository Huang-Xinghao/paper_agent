import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.role_sessions import PhaseTask, RoleSession
from paper_agent.agent.user_questions import UserQuestions, QuestionInterrupted, answer_command


class Model:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.steps)


def call(name, **args):
    return ModelResponse(tool_calls=(ToolCall(name, name, args),))


def test_role_question_continues_same_task_and_keeps_prior_tool_receipt(tmp_path):
    store = UserQuestions(tmp_path)
    events = []
    def event(kind, record):
        events.append((kind, record))
        if kind == 'user_question':
            question = record['question']
            assert (tmp_path / 'before.md').read_text() == 'done once'
            assert question['role'] == 'idea_generator'
            record = RoleSession(tmp_path, question['role']).load()['tasks'][-1]
            assert record['id'] == question['task_id'] and record['status'] == 'awaiting_user'
            with pytest.raises(ValueError, match='/answer'):
                answer_command(tmp_path, '现在进度怎样？')
            assert len(store.pending()) == 1
            answer_command(tmp_path, '/answer ' + question['id'] + ' 采用方案 B')
            with pytest.raises(ValueError, match='已回答'):
                store.answer(question['id'], '重复答案')

    model = Model(call('write', path='before.md', content='done once'),
                  call('request_user_decision', question='用哪个方案？', options=['方案 A', '方案 B']),
                  call('write', path='after.md', content='采用方案 B'), ModelResponse(content='完成'))
    state = PhaseAgent(tmp_path, model_client=model, on_progress=event).execute(
        PhaseTask('ideas', '先写文件，然后询问方案，再继续'), completion_check=lambda: None)
    receipt = model.requests[2][-1]
    assert receipt['name'] == 'request_user_decision'
    assert json.loads(receipt['content'])['answer'] == '采用方案 B'
    assert [m['name'] for m in state.task_messages if m['role'] == 'tool'] == ['write', 'request_user_decision', 'write']
    assert len(RoleSession(tmp_path, 'idea_generator').load()['tasks']) == 1
    assert store.pending() == []
    question = next(p for p in store.root.glob('*.json'))
    assert json.loads(question.read_text())['status'] == 'consumed'


def test_main_live_question_resumes_dispatch_after_explicit_answer(tmp_path, monkeypatch):
    from paper_agent.agent import coordinator
    from paper_agent.agent.workflow import WorkflowState
    dispatched = []
    def execute(project, node, instruction, **options):
        dispatched.append(node)
        return WorkflowState('test', project_goal='Research', current_node=node, status='completed')
    monkeypatch.setattr(coordinator, 'execute_stage', execute)
    def event(record):
        if record['kind'] == 'user_question':
            assert dispatched == []
            answer_command(tmp_path, '/answer 采用 B')
    model = Model(call('request_user_decision', question='选择路线', options=['A', 'B']),
                  call('delegate_stage', assignment={'node': 'literature', 'role': 'idea_generator',
                                                    'instruction': '执行 B 路线'}),
                  ModelResponse(content='B 完成'))
    result = CoordinatorAgent(tmp_path, model_client=model, wait_for_user=True, on_event=event).go('Research')
    assert result.status == 'completed' and dispatched == ['literature']
    assert json.loads(model.requests[1][-1]['content'])['answer'] == '采用 B'
    assert len(RoleSession(tmp_path, 'main').load()['tasks']) == 1


def test_status_query_does_not_answer_or_allow_dispatch(tmp_path):
    CoordinatorAgent(tmp_path, model_client=Model(call('request_user_decision', question='选 A 还是 B？'),
                                                ModelResponse(content='请回答'))).go('Research')
    model = Model(call('delegate_stage', assignment={'node': 'literature', 'role': 'idea_generator',
                                                    'instruction': '禁止执行'}), ModelResponse(content='仍等待回答'))
    result = CoordinatorAgent(tmp_path, model_client=model).go('现在进度怎样？')
    assert result.status == 'awaiting_user'
    assert UserQuestions(tmp_path).pending()[0]['status'] == 'awaiting_user'
    assert not (tmp_path / 'literature').exists()


def test_stale_answer_interrupts_wait_instead_of_continuing(tmp_path):
    path = tmp_path / 'PAPER_PLAN.md'
    path.write_text('version 1')
    store = UserQuestions(tmp_path)
    value = store.create('选哪条路线？', role='paper_writer', task_id='original', phase='paper_plan')
    path.write_text('version 2')
    with pytest.raises(ValueError, match='版本已变化'):
        store.answer(value['id'], 'B')
    with pytest.raises(QuestionInterrupted):
        store.wait(value, lambda *a, **k: None)
    assert store.read(value['id'])['status'] == 'stale'


def test_interrupted_wait_invalidates_question_and_rejects_late_answer(tmp_path):
    store = UserQuestions(tmp_path)
    value = store.create('需要输入', role='experiment', task_id='test', phase='experiment_bridge')
    def stop(*a, **k):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        store.wait(value, stop)
    assert store.read(value['id'])['status'] == 'interrupted'
    with pytest.raises(ValueError, match='失效'):
        store.answer(value['id'], '不应接收')


def test_version_changes_after_answer_before_consumption_still_stop_execution(tmp_path):
    path = tmp_path / 'PAPER_PLAN.md'
    path.write_text('original')
    store = UserQuestions(tmp_path)
    question = store.create('确认路线', role='paper_writer', task_id='task', phase='paper_plan')
    store.answer(question['id'], 'B')
    path.write_text('edited before resume')
    with pytest.raises(QuestionInterrupted, match='changed'):
        store.wait(question, lambda *a, **k: None)
    assert store.read(question['id'])['status'] == 'interrupted'


@pytest.mark.parametrize('role', ['main', 'idea_generator', 'delegated'])
def test_wait_can_be_cancelled_by_sigint_without_stranded_thread(tmp_path, role):
    script = '''
import sys
from paper_agent.agent.coordinator import CoordinatorAgent
from paper_agent.agent.model_client import ModelResponse, ToolCall
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.role_sessions import PhaseTask
from paper_agent.agent import coordinator, workflow_cli
from paper_agent.agent.workflow import WorkflowRunner
class Model:
    def complete(self, messages, tools):
        return ModelResponse(tool_calls=(ToolCall('ask', 'request_user_decision', {'question': 'choose'}),))
class Delegator:
    def complete(self, messages, tools):
        return ModelResponse(tool_calls=(ToolCall('assign', 'delegate_stage', {'assignment': {
            'node': 'literature', 'role': 'idea_generator', 'instruction': 'Research'}}),))
try:
    if sys.argv[2] == 'main':
        CoordinatorAgent(sys.argv[1], model_client=Model(), wait_for_user=True).go('Research')
    elif sys.argv[2] == 'delegated':
        def make_agent(project, **kwargs):
            kwargs.pop('model', None)
            return PhaseAgent(project, model_client=Model(), **kwargs)
        workflow_cli.PhaseAgent = make_agent
        WorkflowRunner._research_context = lambda *a, **k: {}
        coordinator.execute_stage = workflow_cli.execute_stage
        CoordinatorAgent(sys.argv[1], model_client=Delegator()).go('Research')
    else:
        PhaseAgent(sys.argv[1], model_client=Model()).execute(PhaseTask('ideas', 'Research'), completion_check=lambda: None)
except KeyboardInterrupt:
    pass
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), role], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not UserQuestions(tmp_path).pending():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        os.kill(process.pid, signal.SIGINT)
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, (out, err)
        assert not UserQuestions(tmp_path).pending()
        session_role = 'main' if role == 'delegated' else role
        assert RoleSession(tmp_path, session_role).load()['tasks'][-1]['status'] == 'interrupted'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_main_ui_answers_while_busy_and_status_preserves_question(tmp_path):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from paper_agent.agent.main_terminal import MainTerminal

    async def exercise():
        root = tmp_path / '.paper-agent/main-terminal'
        root.mkdir(parents=True)
        store = UserQuestions(tmp_path)
        question = store.create('选 A 还是 B？', role='experiment', task_id='task',
                                phase='experiment_plan', options=['A', 'B'])
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            ui = MainTerminal(tmp_path, root, lambda s: None)
            app = asyncio.create_task(ui.app.run_async())
            try:
                async with asyncio.timeout(3):
                    while not ui.app.is_running:
                        await asyncio.sleep(.01)
                ui.busy, ui.started = True, time.monotonic()
                ui.event({'kind': 'user_question', 'question': question, 'role': 'experiment'})
                assert 'Experiment 需要你回答' in ui.transcript.text
                ui.editor.text = '/status'
                ui.submit()
                assert len(store.pending()) == 1 and ui.busy
                ui.editor.text = '现在进度怎样'
                ui.submit()
                assert ui.editor.text == '现在进度怎样' and len(store.pending()) == 1
                ui.editor.text = '/answer B'
                ui.submit()
                assert store.read(question['id'])['answer'] == 'B'
                assert ui.busy and ui.pending_question is None
                assert ui.editor.text == ''
            finally:
                ui.app.exit()
                await app
    asyncio.run(exercise())
