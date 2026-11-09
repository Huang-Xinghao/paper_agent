"""Exercise terminal input and task lifecycle with a real prompt-toolkit event loop."""
import asyncio
import json
import threading
from pathlib import Path

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from paper_agent.agent.role_terminal import RoleTerminal, clean
from paper_agent.agent import role_terminal
from paper_agent.agent.workflow_cli import _write_json
from paper_agent.agent.herdr import directory


async def until(condition):
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.02)


def test_chat_multiline_busy_draft_and_keyboard_commands(tmp_path):
    async def exercise():
        started, release = threading.Event(), threading.Event()
        def chat(project, role, message):
            assert message == 'first\nsecond'
            started.set()
            release.wait(4)
            return 'A saved response'
        root = directory(tmp_path) / 'roles/paper_writer'
        root.mkdir(parents=True)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            ui = RoleTerminal(tmp_path, 'paper_writer', root, lambda status: None, chat)
            app = asyncio.create_task(ui.app.run_async())
            try:
                await until(lambda: ui.app.is_running)
                ui.app.layout.focus(ui.transcript)
                pipe.send_text('first\x1b\rsecond\r')
                await until(started.is_set)
                pipe.send_text('keep this draft\r')
                await until(lambda: '草稿已保留' in ui.detail)
                assert ui.editor.text == 'keep this draft'
                release.set()
                await until(lambda: not ui.busy)
                assert 'A saved response' in ui.transcript.text
                assert ui.editor.text == 'keep this draft'
                pipe.send_bytes(b'\x03')
                await until(lambda: not ui.editor.text)
                pipe.send_text('/help\r')
                await until(lambda: 'Ctrl+F' in ui.transcript.text)
                pipe.send_text('/clear\r')
                await until(lambda: not ui.transcript.text)
                ui.app.layout.focus(ui.transcript)
                pipe.send_text('\x1b[200~pasted\nnotes\x1b[201~')
                await until(lambda: ui.editor.text == 'pasted\nnotes')
                assert ui.app.layout.has_focus(ui.editor)
                pipe.send_bytes(b'\x03')
                await until(lambda: not ui.editor.text)
                pipe.send_bytes(b'\x04')
                await asyncio.wait_for(app, 3)
            finally:
                release.set()
                if not app.done():
                    ui.app.exit()
                    await app
    asyncio.run(exercise())


def test_mailbox_task_preserves_draft_and_cancel_waits_for_receipt(tmp_path, monkeypatch):
    async def exercise():
        root = directory(tmp_path) / 'roles/experiment'
        root.mkdir(parents=True)
        folder = directory(tmp_path) / 'tasks' / ('a' * 32)
        _write_json(folder / 'request.json', {'role': 'experiment', 'node': 'experiment_plan', 'instruction': 'Plan'})
        _write_json(root / 'inbox.json', {'id': folder.name})
        release = asyncio.Event()
        class Output:
            lines = 0
            async def readline(self):
                if not self.lines:
                    self.lines += 1
                    return b'node_start\n'
                await release.wait()
                _write_json(folder / 'result.json', {'status': 'interrupted', 'error': 'Cancelled'})
                return b''
        class Process:
            stdout = Output()
            async def wait(self):
                return 0
        async def spawn(*args, **kwargs):
            assert args[2:4] == ('-m', 'paper_agent.agent.herdr_worker')
            return Process()
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            ui = RoleTerminal(tmp_path, 'experiment', root, lambda status: None, lambda *a: '')
            ui.editor.text = 'unsent draft'
            app = asyncio.create_task(ui.app.run_async(pre_run=lambda: ui.app.create_background_task(ui.mailbox())))
            try:
                await until(lambda: ui.process is not None)
                assert ui.editor.text == 'unsent draft'
                assert not (root / 'inbox.json').exists()
                # The worker writes events without printing another stdout line.
                events = folder / 'events.jsonl'
                events.write_text(json.dumps({'kind': 'model_start', 'phase': 'experiment_plan',
                                               'iteration': 3}) + '\n')
                await until(lambda: '正在思考' in ui.activity_label)
                def screen_text():
                    screen = ui.app.renderer.last_rendered_screen
                    return '\n'.join(''.join(cell.char for _, cell in sorted(row.items()))
                                     for _, row in sorted(screen.data_buffer.items())) if screen else ''
                await until(lambda: '正在思考' in screen_text())
                assert '第 3 轮' in screen_text()
                with events.open('a') as stream:
                    stream.write(json.dumps({'kind': 'assistant_delta', 'phase': 'experiment_plan',
                        'iteration': 3, 'content': '正在核对已批准方案的拟合预算。'}) + '\n')
                await until(lambda: '正在核对已批准方案' in ui.transcript.text)
                assert '正在回复' in ui.activity_label
                assert not release.is_set()
                assert ui.editor.text == 'unsent draft'
                pipe.send_bytes(b'\x03')
                await until(lambda: not ui.editor.text)
                pipe.send_bytes(b'\x03')
                await until(lambda: (folder / 'cancel').exists())
                pipe.send_bytes(b'\x04')
                await asyncio.sleep(0.05)
                assert not app.done()  # Never exit underneath an active task.
                release.set()
                await until(lambda: not ui.busy)
                assert 'interrupted' in ui.transcript.text
                assert 'node_start' in ui.latest_log.read_text()
                pipe.send_bytes(b'\x04')
                await asyncio.wait_for(app, 3)
            finally:
                release.set()
                if not app.done():
                    ui.app.exit()
                    await app
    asyncio.run(exercise())


def test_model_and_project_text_cannot_inject_terminal_controls():
    assert clean('hello\x1b[2J\x1b]0;bad title\x07world\x00') == 'helloworld'


def test_live_reply_replaces_retry_fragment_and_avoids_duplicate_final(tmp_path):
    root = directory(tmp_path) / 'roles/paper_writer'
    root.mkdir(parents=True)
    with create_pipe_input(), create_app_session(output=DummyOutput()):
        ui = RoleTerminal(tmp_path, 'paper_writer', root, lambda s: None, lambda *a: '')
        def emit(kind, **data):
            ui.model_event({'kind': kind, 'phase': 'paper_write', 'iteration': 1, **data}, 'Paper Writer')
        emit('assistant_stream_start')
        emit('assistant_delta', content='discarded partial')
        assert 'discarded partial' in ui.transcript.text
        emit('assistant_stream_start')
        emit('assistant_delta', content='new ')
        emit('assistant_delta', content='answer')
        emit('assistant_output', content='new answer', truncated=False)
        assert 'discarded partial' not in ui.transcript.text
        assert ui.transcript.text.count('new answer') == 1
        assert ui.last_output == 'new answer'


def test_activity_elapsed_streaming_heartbeat_and_tool_transitions(tmp_path, monkeypatch):
    root = directory(tmp_path) / 'roles/experiment'
    root.mkdir(parents=True)
    with create_pipe_input(), create_app_session(output=DummyOutput()):
        clock = [100.0]
        monkeypatch.setattr(role_terminal.time, 'monotonic', lambda: clock[0])
        ui = RoleTerminal(tmp_path, 'experiment', root, lambda s: None, lambda *a: '')
        ui.busy, ui.started = True, clock[0]
        def emit(kind, **kwargs):
            record = {'kind': kind, 'phase': 'experiment_plan', 'iteration': 4, **kwargs}
            ui.model_event(record, 'Experiment')
            ui.action_event(record, 'Experiment')
        emit('model_start')
        clock[0] += 65
        assert '正在思考 · 1分05秒' in ''.join(t for _, t in ui.activity())
        count = len(ui.blocks)
        emit('model_heartbeat', waited_seconds=65)
        assert len(ui.blocks) == count
        assert '最近事件 0秒前' in ''.join(t for _, t in ui.activity())
        emit('assistant_stream_start')
        emit('assistant_delta', content='公开进展')
        assert '正在回复' in ui.activity_label
        assert '已接收 4 字' in ''.join(t for _, t in ui.activity())
        emit('model_heartbeat')
        assert '正在回复' in ui.activity_label
        emit('assistant_output', content='公开进展')
        assert ui.transcript.text.count('公开进展') == 1
        emit('tool_start', tool='bash', arguments=json.dumps({'command': 'python audit.py'}))
        assert '运行命令' in ui.activity_label
        assert 'python audit.py' in ui.transcript.text
        emit('tool_end', tool='bash')
        assert '核对工具结果' in ui.activity_label
        emit('reasoning_delta', content='private reasoning must not be displayed')
        assert 'private reasoning' not in ui.transcript.text
        emit('tool_start', tool='read', arguments='invalid JSON')
        assert '读取文件' in ui.activity_label
