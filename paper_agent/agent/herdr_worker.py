"""Run paper-agent itself inside a Herdr pane, without another LLM wrapper."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .herdr import Client, HerdrError, directory, require_managed_pane, role_for_node
from .workflow import IdeaNodeConfig, WorkflowConfig, WorkflowState
from .workflow_cli import _write_json, execute_stage


def run(folder: Path):
    require_managed_pane()
    folder = folder.resolve()
    request = json.loads((folder / 'request.json').read_text())
    project = Path(request['project']).resolve()
    if folder != directory(project) / 'tasks' / request['id'] or role_for_node(request['node']) != request['role']:
        raise ValueError('Invalid Herdr job path or stage')
    with (folder / 'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('Task already has a running worker') from exc
        if (folder / 'result.json').is_file() or (folder / 'started.json').is_file():
            raise HerdrError('Task was already started; inspect its receipt instead of replaying it')
        pane = os.environ['HERDR_PANE_ID']
        active = json.loads((directory(project) / 'active.json').read_text())
        if active.get('id') != request['id'] or active.get('pane_id') != pane:
            raise HerdrError('Worker is not running in the pane assigned to this task')
        _write_json(folder / 'started.json', {'id': request['id'], 'pid': os.getpid(), 'pane_id': pane})
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
            if state:
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
            if (folder / 'cancel').exists():
                raise KeyboardInterrupt
            report('working')
            options = request['options'].copy()
            for key, cls in (('config', WorkflowConfig), ('idea_config', IdeaNodeConfig)):
                if isinstance(options.get(key), dict):
                    options[key] = cls(**options[key])
            state = execute_stage(project, request['node'], request['instruction'],
                                  role=request['role'], project_goal=request['project_goal'],
                                  revise=request['revise'], on_event=emit, **options)
        except (KeyboardInterrupt, SystemExit) as exc:
            path = project / 'workflow-state.json'
            state = WorkflowState.from_dict(json.loads(path.read_text())) if path.is_file() else WorkflowState(
                request['id'], project_goal=request['project_goal'])
            state.status, state.error = 'interrupted', str(exc) or 'Herdr stage was interrupted'
            state.current_node = state.failed_node = request['node']
        except Exception as exc:
            path = project / 'workflow-state.json'
            state = WorkflowState.from_dict(json.loads(path.read_text())) if path.is_file() else WorkflowState(
                request['id'], project_goal=request['project_goal'])
            state.status, state.error = 'failed', str(exc)
            state.current_node = state.failed_node = request['node']
        finally:
            stopped.set()
            watcher.join(timeout=1)
        state.node_results.setdefault(request['node'], {})['execution'] = {
            'backend': 'herdr', 'herdr_task_id': request['id'], 'pane_id': pane}
        path = project / 'workflow-state.json'
        _write_json(path, state.to_dict())
        _write_json(folder / 'result.json', {'id': request['id'], 'status': state.status,
            'error': state.error, 'state_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        print(f"Result: {state.status}" + (f" — {state.error}" if state.error else ''), flush=True)
        report('blocked' if state.status in {'awaiting_user', 'awaiting_results'} else
               'idle' if state.status == 'completed' else 'unknown', state)


if __name__ == '__main__':
    run(Path(sys.argv[1]))
