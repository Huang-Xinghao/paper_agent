"""Run paper-agent itself inside a Herdr pane, without another LLM wrapper."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading

from paper_agent.agent.runtime.herdr import Client, HerdrError, directory, require_managed_pane


def run(folder: Path):
    require_managed_pane()
    folder = folder.resolve()
    request = json.loads((folder / 'request.json').read_text())
    project = Path(request['project']).resolve()
    if folder != directory(project) / 'tasks' / request['id']:
        raise ValueError('Invalid Herdr job path or stage')
    if (folder / 'result.json').exists() or (folder / 'started.json').exists():
        raise HerdrError('Task was already started; inspect its receipt instead of replaying it')
    pane = os.environ['HERDR_PANE_ID']
    active = json.loads((directory(project) / 'active.json').read_text())
    if active.get('id') != request['id'] or active.get('pane_id') != pane or active.get('role') != request['role']:
        raise HerdrError('Worker is not running in the pane assigned to this task')
    print(f"paper-agent | {request['role']} | {request['node']} | task {request['id']}", flush=True)
    client = Client(request['session'], request['binary'])
    stopped = threading.Event()
    state = None

    def emit(record):
        with (folder / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({**record, 'herdr_task_id': request['id']}, ensure_ascii=False) + '\n')

    def report(status, state=None):
        args = ['pane', 'report-agent', pane, '--source', 'role-' + request['role'],
                '--agent', 'paper-agent-' + request['role'], '--state', status]
        if state and hasattr(state, 'node_results'):
            session_id = state.node_results.get(request['node'], {}).get('session_id')
            if session_id:
                args += ['--agent-session-id', session_id]
        try:
            client.call(*args)
        except HerdrError as exc:
            emit({'kind': 'herdr_display_error', 'error': str(exc)})

    def watch_cancel():
        while not stopped.wait(0.5):
            if (folder / 'cancel').exists():
                os.kill(os.getpid(), signal.SIGINT)
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        report('working')
        from paper_agent.agent.runtime.execution import execute_task
        try:
            state = execute_task(folder, execution={
                'backend': 'herdr', 'herdr_task_id': request['id'], 'pane_id': pane})
        except (RuntimeError, BlockingIOError) as exc:
            raise HerdrError(str(exc)) from exc
    finally:
        stopped.set()
        watcher.join(timeout=1)
    print(f"Result: {state.status}" + (f" — {state.error}" if state.error else ''), flush=True)
    report('blocked' if state.status in {'awaiting_user', 'awaiting_results'} else
           'idle' if state.status == 'completed' else 'unknown', state)


if __name__ == '__main__':
    run(Path(sys.argv[1]))
