"""Persistent Herdr role host and adapter for executing its assigned tasks."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import select
import signal
import sys
import threading

from batteryscientist.agent.runtime.herdr import Client, HerdrError, directory, _read, require_managed_pane
from batteryscientist.agent.runtime.sessions import RoleSession
from batteryscientist.agent.stages import ROLE_DESCRIPTIONS
from batteryscientist.storage import write_json as _write_json


def chat(project, role, prompt):
    """Discussion memory is isolated from the role's formal execution memory."""
    from batteryscientist.integrations.model import ModelClient, load_env_file
    load_env_file(Path(__file__).resolve().parents[3] / '.env')
    with (directory(project) / 'transport.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('Main is executing a task; wait for its receipt before chatting') from exc
        from batteryscientist.agent.runtime.herdr import ensure_no_pending
        ensure_no_pending(project)
        session = RoleSession(project, role, channel='discussion')
        with session.locked():
            data = session.load()
            history = session.recover_messages(data['messages'])
            model = ModelClient(model=os.environ.get('BATTERYSCIENTIST_MODEL', 'gpt-4o'), timeout=120)
            model.usage_workspace, model.usage_phase = project, 'role_chat'
            from batteryscientist.agent.context import CONTEXT_MAX_TOKENS
            history = session.compact(data, history, model, CONTEXT_MAX_TOKENS)
            message = {'role': 'user', 'content': prompt}
            response = model.complete([{'role': 'system', 'content': ROLE_DESCRIPTIONS[role] +
                '\n用户在此窗口的对话只进入本角色的讨论上下文，不修改方案或其他MD，不批准方案。'
                '主控可通过角色CLI读取这里的聊天记录；正式任务由主控确认后派发。'
                '只讨论、解释或提出建议；本轮没有执行工具。不得声称执行了文件修改或研究。'
                '要落实建议，请用户在主控窗口说明；主控负责理解、修改MD、提问和接收审批。'}] + history + [message], [])
            data['messages'] = history + [message, {'role': 'assistant', 'content': response.content}]
            session.save(data)
            return response.content


def run_task(folder: Path):
    """Execute one assigned task in this pane and always settle its display state."""
    require_managed_pane()
    folder = folder.resolve()
    request = json.loads((folder / 'request.json').read_text())
    project = Path(request['project']).resolve()
    if folder != directory(project) / 'tasks' / request['id']:
        raise ValueError('Invalid Herdr job path or stage')
    pane = os.environ['HERDR_PANE_ID']
    active = json.loads((directory(project) / 'active.json').read_text())
    if active.get('id') != request['id'] or active.get('pane_id') != pane or active.get('role') != request['role']:
        raise HerdrError('Worker is not running in the pane assigned to this task')
    print(f"batteryscientist | {request['role']} | {request['node']} | task {request['id']}", flush=True)
    client = Client(request['session'], request['binary'])
    stopped = threading.Event()
    state = None

    def record_display_error(error):
        record = {'kind': 'herdr_display_error', 'error': str(error),
                  'herdr_task_id': request['id']}
        try:
            with (folder / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError as exc:
            logging.getLogger(__name__).warning(
                'Could not record Herdr display error (%s): %s', exc, error)

    def report(status, state=None):
        args = ['pane', 'report-agent', pane, '--source', 'role-' + request['role'],
                '--agent', 'batteryscientist-' + request['role'], '--state', status]
        if state and hasattr(state, 'node_results'):
            session_id = state.node_results.get(request['node'], {}).get('session_id')
            if session_id:
                args += ['--agent-session-id', session_id]
        try:
            client.call(*args)
        except HerdrError as exc:
            record_display_error(exc)

    def watch_cancel():
        while not stopped.wait(0.5):
            if (folder / 'cancel').exists():
                os.kill(os.getpid(), signal.SIGINT)
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        report('working')
        from batteryscientist.agent.runtime.execution import execute_task
        try:
            state = execute_task(folder, execution={
                'backend': 'herdr', 'herdr_task_id': request['id'], 'pane_id': pane})
        except (RuntimeError, BlockingIOError) as exc:
            raise HerdrError(str(exc)) from exc
    finally:
        stopped.set()
        try:
            watcher.join(timeout=1)
        finally:
            status = 'unknown'
            if state is not None:
                status = ('blocked' if state.status in {'awaiting_user', 'awaiting_results'} else
                          'idle' if state.status == 'completed' else 'unknown')
            report(status, state)
    print(f"Result: {state.status}" + (f" — {state.error}" if state.error else ''), flush=True)


def serve(root):
    require_managed_pane()
    root = Path(root).resolve()
    binding = _read(root / 'binding.json')
    project, role = Path(binding['project']).resolve(), binding['role']
    if (role not in {'idea_generator', 'experiment', 'paper_writer', 'wiki_agent'}
            or root != directory(project) / 'roles' / role
            or binding['pane_id'] != os.environ['HERDR_PANE_ID']):
        raise HerdrError('Role host identity mismatch')
    with (root / 'host.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = Client(binding['session'], binding['binary'])
        def report(status):
            try:
                client.call('pane', 'report-agent', binding['pane_id'], '--source', 'role-' + role,
                            '--agent', 'batteryscientist-' + role, '--state', status)
            except HerdrError as exc:
                logging.getLogger(__name__).warning('Could not update Herdr host display: %s', exc)
        client.call('pane', 'rename', binding['pane_id'], role + ' · BatteryScientist')
        report('idle')
        name = 'pa-' + hashlib.sha256(str(project).encode()).hexdigest()[:8] + '-' + role
        client.call('agent', 'rename', binding['pane_id'], name)
        _write_json(root / 'ready.json', {'generation': binding['generation'], 'pid': os.getpid()})
        if sys.stdin.isatty() and sys.stdout.isatty():
            from batteryscientist.agent.terminal.role import RoleTerminal
            try:
                RoleTerminal(project, role, root, report, chat).run()
            finally:
                (root / 'ready.json').unlink(missing_ok=True)
                report('unknown')
            return
        print(f'\n╭─ BatteryScientist · {role}\n│ Project: {project}\n│ /help 帮助 · /status 状态 · /quit 退出\n│ 输入文字与此角色对话；阶段执行由主控派发。\n╰──────────────────────────────────────────', flush=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='Role host directory, or task directory with --task')
    parser.add_argument('--task', action='store_true', help='Execute one task instead of hosting a role')
    args = parser.parse_args()
    if args.task:
        run_task(args.path)
    else:
        serve(args.path)


if __name__ == '__main__':
    main()
