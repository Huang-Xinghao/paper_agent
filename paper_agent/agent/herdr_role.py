"""Persistent, project-owned interactive role host for Herdr."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sys

from .herdr import Client, HerdrError, directory, _read, require_managed_pane
from .herdr_worker import run as run_task
from .workflow_cli import _write_json
from .role_sessions import RoleSession, ROLE_DESCRIPTIONS


def chat(project, role, prompt):
    """Discussion memory is isolated from the role's formal execution memory."""
    from .model_client import ModelClient, load_env_file
    load_env_file(Path(__file__).resolve().parents[2] / '.env')
    with (directory(project) / 'transport.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('Main is executing a task; wait for its receipt before chatting') from exc
        from .herdr import ensure_no_pending
        ensure_no_pending(project)
        session = RoleSession(project, role, channel='discussion')
        with session.locked():
            data = session.load()
            history = session.recover_messages(data['messages'])
            model = ModelClient(model=os.environ.get('PAPER_AGENT_MODEL', 'gpt-4o'), timeout=120)
            model.usage_workspace, model.usage_phase = project, 'role_chat'
            history = session.compact(data, history, model, 60000)
            message = {'role': 'user', 'content': prompt}
            response = model.complete([{'role': 'system', 'content': ROLE_DESCRIPTIONS[role] +
                '\n这是独立的讨论会话，不会写入正式任务记忆。只讨论、解释或提出建议；本轮没有执行工具。'
                '不得声称执行了文件修改或研究。要落实建议，请用户将修改要求交给主控派发。'}] + history + [message], [])
            data['messages'] = history + [message, {'role': 'assistant', 'content': response.content}]
            session.save(data)
            return response.content


def serve(root):
    require_managed_pane()
    root = Path(root).resolve()
    binding = _read(root / 'binding.json')
    project, role = Path(binding['project']).resolve(), binding['role']
    if (role not in {'idea_generator', 'experiment', 'paper_writer'}
            or root != directory(project) / 'roles' / role
            or binding['pane_id'] != os.environ['HERDR_PANE_ID']):
        raise HerdrError('Role host identity mismatch')
    with (root / 'host.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = Client(binding['session'], binding['binary'])
        def report(status):
            client.call('pane', 'report-agent', binding['pane_id'], '--source', 'role-' + role,
                        '--agent', 'paper-agent-' + role, '--state', status)
        client.call('pane', 'rename', binding['pane_id'], role + ' · Paper Agent')
        report('idle')
        name = 'pa-' + hashlib.sha256(str(project).encode()).hexdigest()[:8] + '-' + role
        client.call('agent', 'rename', binding['pane_id'], name)
        _write_json(root / 'ready.json', {'generation': binding['generation'], 'pid': os.getpid()})
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .role_terminal import RoleTerminal
            try:
                RoleTerminal(project, role, root, report, chat).run()
            finally:
                (root / 'ready.json').unlink(missing_ok=True)
                report('unknown')
            return
        print(f'\n╭─ Paper Agent · {role}\n│ Project: {project}\n│ /help 帮助 · /status 状态 · /quit 退出\n│ 输入文字与此角色对话；阶段执行由主控派发。\n╰──────────────────────────────────────────', flush=True)
        prompt = True
        try:
            while True:
                inbox = _read(root / 'inbox.json')
                if inbox:
                    task_id = inbox['id']
                    if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
                        raise HerdrError('Invalid mailbox task ID')
                    folder = directory(project) / 'tasks' / task_id
                    request = _read(folder / 'request.json')
                    if request['role'] != role:
                        raise HerdrError('Task belongs to another role')
                    # Claim before running: main may submit the next task as soon
                    # as the receipt appears, before display reporting finishes.
                    (root / 'inbox.json').unlink()
                    if not (folder / 'result.json').exists():
                        print(f'\n[主控派单] {request["node"]}: {request["instruction"]}', flush=True)
                        run_task(folder)
                    prompt = True
                if prompt:
                    print(f'\n{role} › ', end='', flush=True)
                    prompt = False
                if not select.select([sys.stdin], [], [], 0.2)[0]:
                    continue
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line == '/quit':
                    break
                if line == '/help':
                    print('文字：与角色讨论（独立讨论记忆，不影响正式任务）\n/status：查看工作流状态\n/quit：退出当前角色进程', flush=True)
                elif line == '/status':
                    state = _read(project / 'workflow-state.json') or {}
                    print(json.dumps({k: state.get(k) for k in ('status', 'current_node', 'project_goal', 'error')}, ensure_ascii=False), flush=True)
                elif line:
                    try:
                        report('working')
                        print('\n' + chat(project, role, line), flush=True)
                    except Exception as exc:
                        print(f'\n对话未完成：{exc}', flush=True)
                    finally:
                        report('idle')
                prompt = True
        finally:
            (root / 'ready.json').unlink(missing_ok=True)
            report('unknown')


if __name__ == '__main__':
    serve(Path(sys.argv[1]))
