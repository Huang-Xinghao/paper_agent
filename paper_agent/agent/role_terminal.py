"""Minimal full-screen terminal for a persistent research role."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys
import time

from prompt_toolkit import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, SearchToolbar

from .herdr import directory, _read, _relay, HerdrError
from .role_sessions import RoleSession


TITLES = {'main': 'Main', 'idea_generator': 'Idea Generator', 'experiment': 'Experiment', 'paper_writer': 'Paper Writer'}
HELP = ('Enter 发送 · Alt+Enter / Ctrl+J 换行 · Tab 补全命令\n'
        'PgUp/PgDn 浏览消息 · Ctrl+F 搜索 · Esc 返回输入\n'
        '/status 项目状态   /logs 执行日志   /clear 清屏   /quit 退出\n'
        'Ctrl+C 清空草稿；执行阶段时请求取消。直接对话只讨论，阶段任务由主控派发。'
        '\n角色讨论记忆与正式执行记忆独立；要落实修改，请交给 Main。')


def clean(text):
    # Project/model text is data; never render terminal escape sequences.
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(text))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60}分{seconds % 60:02d}秒' if seconds >= 60 else f'{seconds}秒'


async def follow_process(folder, process, on_event):
    """Read durable events even when a model or tool produces no stdout."""
    async def drain():
        with (folder / 'terminal.log').open('ab') as log:
            while line := await process.stdout.readline():
                log.write(line)
                log.flush()
    output = asyncio.create_task(drain())
    offset = 0
    try:
        while not output.done():
            offset = _relay(folder, offset, on_event)
            await asyncio.wait({output}, timeout=0.1)
        await output
        code = await process.wait()
        _relay(folder, offset, on_event)
        return code
    finally:
        if not output.done():
            output.cancel()
        await asyncio.gather(output, return_exceptions=True)


class TranscriptLexer(Lexer):
    def lex_document(self, document):
        def line(i):
            value = document.lines[i]
            style = ('class:user' if value.startswith('› ') else
                     'class:assistant' if value.startswith('● ') else
                     'class:muted' if value.startswith('· ') else '')
            return [(style, value)]
        return line


class RoleTerminal:
    def __init__(self, project, role, root, report, chat):
        self.project, self.role, self.root = Path(project), role, Path(root)
        self.report, self.chat = report, chat
        self.help_text = HELP
        self.busy = False
        self.detail = '就绪'
        self.started = 0.0
        self.folder = None
        self.process = None
        self.latest_log = None
        self.closing = False
        self.confirm_quit = False
        self.blocks = []
        self._stream_index = None
        self._stream_key = None
        self._stream_text = ''
        self.last_output = ''
        self.activity_label = '正在准备'
        self.activity_started = self.activity_updated = time.monotonic()
        self.activity_key = None
        self.activity_iteration = None
        self.activity_chars = 0
        self.search = SearchToolbar()
        self.transcript = TextArea(read_only=True, scrollbar=True, wrap_lines=True,
            lexer=TranscriptLexer(), search_field=self.search, name='conversation', height=Dimension(min=1))
        self.editor = TextArea(multiline=True, height=Dimension(min=1, max=5), prompt='› ',
            history=FileHistory(str(self.root / 'input-history')),
            completer=WordCompleter(['/help', '/status', '/logs', '/clear', '/quit', '/answer'], sentence=True),
            complete_while_typing=Condition(lambda: self.editor.text.startswith('/')),
            name='message', style='class:composer')
        keys = KeyBindings()

        @keys.add('<any>', filter=Condition(lambda: self.app.layout.has_focus(self.transcript)))
        def type_from_transcript(event):
            if event.data and event.data.isprintable():
                self.app.layout.focus(self.editor)
                self.editor.buffer.insert_text(event.data)

        @keys.add('enter', filter=Condition(lambda: self.app.layout.has_focus(self.transcript)))
        def input_from_transcript(event):
            self.app.layout.focus(self.editor)

        @keys.add('<bracketed-paste>', filter=Condition(lambda: self.app.layout.has_focus(self.transcript)))
        def paste_from_transcript(event):
            self.app.layout.focus(self.editor)
            self.editor.buffer.insert_text(event.data)

        @keys.add('enter', filter=Condition(lambda: self.app.layout.has_focus(self.editor)))
        def send(event):
            self.submit()

        @keys.add('escape', 'enter', filter=Condition(lambda: self.app.layout.has_focus(self.editor)))
        @keys.add('c-j', filter=Condition(lambda: self.app.layout.has_focus(self.editor)))
        def newline(event):
            self.editor.buffer.insert_text('\n')

        @keys.add('c-c')
        def interrupt(event):
            if self.editor.text:
                self.editor.text = ''
            elif self.folder and self.busy:
                (self.folder / 'cancel').touch()
                self.set_activity('正在请求取消，等待执行回执')
            elif self.busy:
                self.detail = '对话请求中，等待模型返回；输入草稿仍可编辑'
            else:
                self.confirm_quit = not self.confirm_quit
                if self.confirm_quit:
                    self.detail = '再按一次 Ctrl+C 退出，或继续输入'
                else:
                    self.app.exit()

        @keys.add('c-d')
        def quit_app(event):
            self.quit()

        @keys.add('escape')
        def focus_editor(event):
            self.app.layout.focus(self.editor)

        @keys.add('pageup')
        def previous(event):
            self.app.layout.focus(self.transcript)
            self.transcript.buffer.cursor_up(count=10)

        @keys.add('pagedown')
        def following(event):
            self.app.layout.focus(self.transcript)
            self.transcript.buffer.cursor_down(count=10)

        @keys.add('c-f')
        def find(event):
            from prompt_toolkit.search import start_search
            self.app.layout.focus(self.transcript)
            start_search(self.transcript.control)

        style = Style.from_dict({
            'title': 'bold', 'accent': '#8eb7c8', 'muted': '#858b92',
            'user': '#9ac7a1 bold', 'assistant': 'bold', 'rule': '#4b5056',
            'composer': 'bg:#25282d', 'ready': '#9ac7a1', 'working': '#e0bd83',
            'completion-menu.completion': 'bg:#292d33 #c8cdd3',
            'completion-menu.completion.current': 'bg:#3c4652 #ffffff',
            'scrollbar.background': '#30343a', 'scrollbar.button': '#68717d',
        })
        def roomy(rows):
            return Condition(lambda: self.app.output.get_size().rows >= rows)
        body = HSplit([
            Window(FormattedTextControl(self.header), height=1),
            ConditionalContainer(Window(FormattedTextControl(lambda: [('class:muted', f'  {self.project.name}  ·  角色会话已保存')]), height=1), roomy(12)),
            ConditionalContainer(Window(height=1), roomy(16)), self.transcript,
            self.search,
            ConditionalContainer(Window(FormattedTextControl(self.activity), height=2),
                                 Condition(lambda: self.busy)),
            ConditionalContainer(Window(FormattedTextControl(lambda: [('class:rule', '─' * max(1, self.app.output.get_size().columns))]), height=1), roomy(7)),
            self.editor,
            ConditionalContainer(Window(FormattedTextControl(self.footer), height=1), roomy(8)),
        ])
        self.app = Application(layout=Layout(body, focused_element=self.editor), key_bindings=keys,
            style=style, full_screen=True, mouse_support=True, refresh_interval=0.25)
        self.restore()

    def header(self):
        width = self.app.output.get_size().columns
        elapsed = f' · {int(time.monotonic() - self.started)}s' if self.busy else ''
        icon = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[int(time.monotonic() * 6) % 10] if self.busy else '●'
        return [('class:title', '  Paper Agent' if width >= 60 else ' PA'), ('class:muted', ' / '),
                ('class:accent', TITLES[self.role]),
                ('class:working' if self.busy else 'class:ready', f'   {icon} '),
                ('class:muted', clean(self.detail)[:70] + elapsed if width >= 45 else '')]

    def footer(self):
        hint = '  Enter 发送   Alt+Enter 换行   Esc 输入   /help 帮助'
        if self.app.output.get_size().columns < 60:
            hint = ' Enter 发送 · Esc 输入 · /help'
        if self.busy:
            hint = '  执行中 · 草稿保留   Ctrl+C 请求取消' if self.folder else '  等待回复 · 可继续编辑草稿'
        return [('class:muted', hint)]

    def set_activity(self, label, record=None):
        record = record or {}
        key = (label, record.get('phase'), record.get('iteration'), record.get('task_id'))
        if key != self.activity_key:
            self.activity_started = time.monotonic()
            self.activity_chars = 0
        self.activity_key, self.activity_label = key, label
        self.activity_iteration = record.get('iteration', self.activity_iteration)
        self.activity_updated = time.monotonic()
        self.detail = label
        self.app.invalidate()

    def activity(self):
        now = time.monotonic()
        spinner = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[int(now * 6) % 10]
        text = f' {spinner} {clean(self.activity_label)} · {duration(now - self.activity_started)}'
        parts = []
        if self.activity_iteration:
            parts.append(f'第 {self.activity_iteration} 轮')
        if self.activity_chars:
            parts.append(f'已接收 {self.activity_chars} 字')
        parts.extend([f'本轮 {duration(now - self.started)}',
                      f'最近事件 {duration(now - self.activity_updated)}前'])
        return [('class:working', text + '\n'), ('class:muted', '   ' + ' · '.join(parts))]

    def append(self, label, content):
        self.blocks.append(f'{label}\n{clean(content).strip()}\n')
        removed = max(0, len(self.blocks) - 100)
        if self._stream_index is not None:
            self._stream_index -= removed
            if self._stream_index < 0:
                self._stream_index = None
        self.blocks = self.blocks[-100:]
        self.render()

    def render(self):
        self.transcript.text = '\n'.join(self.blocks)[-120000:]
        self.transcript.buffer.cursor_position = len(self.transcript.text)
        self.app.invalidate()

    def model_event(self, record, title):
        """Render only public response text; never provider reasoning fields."""
        kind = record.get('kind')
        key = (title, record.get('task_id'), record.get('phase'), record.get('iteration'))
        if kind == 'assistant_stream_start':
            if self._stream_index is not None:
                self.blocks.pop(self._stream_index)
                self.render()
            self._stream_index, self._stream_key, self._stream_text = None, key, ''
        elif kind == 'assistant_delta':
            if self._stream_key != key:
                self.model_event({**record, 'kind': 'assistant_stream_start'}, title)
            if self._stream_index is None:
                self.append('● ' + title, '')
                self._stream_index = len(self.blocks) - 1
            self._stream_text += clean(record.get('content', ''))
            self.activity_chars = len(self._stream_text)
            self.blocks[self._stream_index] = f'● {title}\n{self._stream_text}\n'
            self.render()
        elif kind == 'assistant_output':
            content = record.get('content', '')
            if self._stream_index is None:
                self.append('● ' + title, content)
            else:
                if not record.get('truncated'):
                    self.blocks[self._stream_index] = f'● {title}\n{clean(content)}\n'
                self._stream_index = None
                self.render()
            self.last_output = content
        elif kind == 'model_error' and self._stream_index is not None:
            self.blocks[self._stream_index] = '· 未完成的回复（不作为执行结果）\n' + self._stream_text + '\n'
            self._stream_index = None
            self.render()

    def action_event(self, record, title):
        kind = record.get('kind')
        if kind == 'model_start':
            self.set_activity(title + ' · 正在思考', record)
        elif kind == 'context_prepared':
            self.set_activity(title + ' · 正在思考，等待模型响应', record)
        elif kind == 'working_context_compacted':
            self.set_activity(title + ' · 已整理上下文，继续思考', record)
        elif kind == 'assistant_stream_start':
            self.set_activity(title + ' · 正在思考，等待模型响应', record)
        elif kind in {'assistant_delta', 'assistant_output'}:
            self.set_activity(title + ' · 正在回复', record)
            self.activity_chars = len(self._stream_text) if kind == 'assistant_delta' else len(record.get('content', ''))
        elif kind == 'model_heartbeat':
            self.activity_updated = time.monotonic()
            self.app.invalidate()
        elif kind == 'model_end':
            self.set_activity(title + ' · 已收到回复，正在处理', record)
        elif kind == 'model_error':
            self.set_activity(title + ' · 模型请求未完成', record)
        elif kind == 'tool_start':
            tool = record.get('tool', '')
            names = {'get_project_context': '查看项目状态', 'read_project_file': '读取文件',
                     'read': '读取文件', 'write': '写入文件', 'edit': '编辑文件',
                     'bash': '运行命令', 'search_wiki': '查询知识库', 'web_search': '检索资料'}
            action = names.get(tool, tool)
            try:
                args = record.get('arguments', {})
                args = json.loads(args) if isinstance(args, str) else args
                path = args.get('path', '')
            except (ValueError, TypeError, AttributeError):
                args = {}
                path = ''
            self.set_activity(title + ' · ' + action, record)
            if tool != 'delegate_stage':
                command = args.get('command', args.get('cmd', '')) if isinstance(args, dict) else ''
                self.append('· ' + title + ' / ' + action, str(path or command)[:300])
        elif kind == 'tool_end':
            self.set_activity(title + ' · 正在核对工具结果', record)
            if record.get('is_error'):
                self.append('· ' + title + ' / 操作未完成', record.get('tool', ''))
        elif kind == 'user_question' and self.role != 'main':
            self.append('◆ 问题已提交给 Main', record['question']['question'] + '\n请在 Main 用 /answer 回答。')
            self.set_activity('等待 Main 转交用户回答', record)
        elif kind == 'user_question_answered' and self.role != 'main':
            self.set_activity('用户已回答，继续当前任务', record)

    def restore(self):
        session = RoleSession(self.project, self.role, channel='execution' if self.role == 'main' else 'discussion')
        data = _read(session.path) or {}
        for message in data.get('messages', [])[-24:]:
            content = message.get('content')
            if message.get('role') in {'user', 'assistant'} and isinstance(content, str) and content.strip():
                self.append('› 你' if message['role'] == 'user' else '● ' + TITLES[self.role], content[:6000])
        if not self.blocks:
            welcome = ('告诉我你的研究目标，或想推进的具体任务。\n我会按需启动角色 Agent、分配任务并核对结果。'
                       if self.role == 'main' else '一起推进研究。\n输入问题与我讨论，或等待主控分配阶段任务。')
            self.append('● ' + TITLES[self.role], welcome + '\n\n输入 /help 查看快捷键。')

    def quit(self):
        if self.busy:
            self.detail = '任务仍在执行；请先取消并等待结束，再退出'
            return
        self.closing = True
        self.app.exit()

    def submit(self):
        value = self.editor.text.strip()
        if not value:
            return
        if value == '/quit':
            self.quit()
            return
        if value.startswith('/'):
            if value == '/help':
                self.append('· 快捷键', self.help_text)
            elif value == '/status':
                state = _read(self.project / 'workflow-state.json') or {}
                self.append('· 项目状态', '\n'.join(f'{key}: {state.get(key) or "—"}' for key in ('status', 'current_node', 'project_goal', 'error')))
                from .artifact_versions import ArtifactVersions
                from .user_questions import UserQuestions
                stale = ArtifactVersions(self.project).context()['stale_nodes']
                if stale:
                    self.append('· 需要重新核查的产物', ', '.join(stale))
                for question in UserQuestions(self.project).pending():
                    self.append('◆ 待回答问题 · ' + TITLES[question['role']], question['question'] +
                                '\n问题 ID: ' + question['id'] + '\n在 Main 输入 /answer 回答内容')
            elif value == '/logs':
                text = self.latest_log.read_text(errors='replace')[-12000:] if self.latest_log and self.latest_log.exists() else '本次会话还没有执行日志。'
                self.append('· 最近执行日志', text)
            elif value == '/clear':
                self.blocks.clear()
                self._stream_index = None
                self.transcript.text = ''
            else:
                self.append('· 未知命令', '输入 /help 查看可用命令。')
            self.editor.text = ''
            return
        if self.busy:
            self.detail = '当前任务未结束，草稿已保留'
            return
        self.confirm_quit = False
        self.editor.buffer.append_to_history()
        self.editor.text = ''
        self.append('› 你', value)
        self.busy, self.started = True, time.monotonic()
        self.activity_iteration = None
        self.set_activity(TITLES[self.role] + ' · 正在准备')
        self.app.create_background_task(self.converse(value))

    async def converse(self, value):
        try:
            self.set_activity(TITLES[self.role] + ' · 正在思考')
            await asyncio.to_thread(self.report, 'working')
            reply = await asyncio.to_thread(self.chat, self.project, self.role, value)
            self.append('● ' + TITLES[self.role], reply)
            self.detail = '就绪'
        except Exception as exc:
            self.append('· 对话未完成', str(exc))
            self.detail = '请求失败，可重新发送'
            if not self.editor.text:
                self.editor.text = value
        finally:
            try:
                await asyncio.to_thread(self.report, 'idle')
            except Exception:
                self.detail = 'Herdr 状态上报暂不可用'
            self.busy = False
            self.app.invalidate()

    async def execute(self, folder, request):
        self.folder, self.started = folder, time.monotonic()
        self.latest_log = folder / 'terminal.log'
        self.append('· 主控派单 / ' + request['node'], request['instruction'])
        self.set_activity('正在执行 ' + request['node'])
        def event(record):
            self.model_event(record, TITLES[self.role])
            self.action_event(record, TITLES[self.role])
        try:
            self.process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-m',
                'paper_agent.agent.herdr_worker', str(folder), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            code = await follow_process(folder, self.process, event)
            receipt = _read(folder / 'result.json')
            if receipt:
                self.append('· 执行回执', receipt['status'] + ('\n' + receipt['error'] if receipt.get('error') else ''))
                self.detail = '等待审阅' if receipt['status'] == 'awaiting_user' else '就绪'
            else:
                self.append('· 执行结果待核查', f'进程退出码 {code}，缺少回执。请检查 /logs；不会自动重跑。')
                self.detail = '结果待核查'
        except Exception as exc:
            self.append('· 执行异常', str(exc))
            self.detail = '结果待核查'
        finally:
            self.process = None
            self.folder = None
            self.busy = False
            self.app.invalidate()

    async def mailbox(self):
        while not self.closing:
            if not self.busy:
                try:
                    inbox = _read(self.root / 'inbox.json')
                    if inbox:
                        task = inbox['id']
                        if not re.fullmatch('[a-f0-9]{32}', task):
                            raise HerdrError('Invalid mailbox task ID')
                        folder = directory(self.project) / 'tasks' / task
                        request = _read(folder / 'request.json')
                        if request['role'] != self.role:
                            raise HerdrError('Task belongs to another role')
                        (self.root / 'inbox.json').unlink()
                        if not (folder / 'result.json').exists():
                            self.busy = True
                            self.app.create_background_task(self.execute(folder, request))
                except Exception as exc:
                    self.append('· 派单读取失败', str(exc))
                    return
            await asyncio.sleep(0.2)

    def run(self):
        def start():
            self.app.create_background_task(self.mailbox())
        self.app.run(pre_run=start)
