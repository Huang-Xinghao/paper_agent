"""Herdr transports single-stage Python jobs; artifacts remain the source of truth."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from uuid import uuid4

from .workflow_cli import _write_json, WORKFLOW_NODE_NAMES
from .workflow import WorkflowState


class HerdrError(RuntimeError):
    pass


def require_managed_pane():
    if os.environ.get('HERDR_ENV') != '1' or not os.environ.get('HERDR_PANE_ID'):
        raise HerdrError('Run inside a Herdr-managed pane; HERDR_ENV=1 and caller pane are required')


class Client:
    def __init__(self, session, binary):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', session):
            raise ValueError('An explicit Herdr session name is required')
        self.session, self.binary = session, binary

    def call(self, *args):
        require_managed_pane()
        try:
            result = subprocess.run([self.binary, '--session', self.session, *args],
                                    capture_output=True, text=True, timeout=30)
            # Herdr 0.9.5 acknowledges these commands with exit 0 and no output.
            # This means queued, never that the worker or task has completed.
            if result.returncode == 0 and not result.stdout.strip():
                if args[:2] == ('pane', 'run'):
                    return {'queued': True}
                if args[:2] in {('pane', 'report-agent'), ('pane', 'rename'), ('agent', 'rename'), ('pane', 'zoom')}:
                    return {}
            value = json.loads(result.stdout if result.returncode == 0 else result.stderr)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise HerdrError(f'Herdr response unknown; reconcile before retrying: {exc}') from exc
        if result.returncode or value.get('error'):
            raise HerdrError(str(value.get('error', value)))
        return value.get('result', value)


def directory(project):
    return Path(project).resolve() / '.paper-agent' / 'herdr'


def connect(project, session, binary=None, direction='down', wait_seconds=7200):
    require_managed_pane()
    project = Path(project).resolve()
    active = _read(directory(project) / 'active.json')
    if active and not active.get('consumed'):
        raise HerdrError('Reconcile the pending task before changing the Herdr binding')
    binary = binary or str(Path(sys.executable).with_name('herdr'))
    client = Client(session, binary)
    pane = client.call('pane', 'current', '--current')['pane']
    # An inherited caller ID must resolve in the explicitly chosen server.
    if pane['pane_id'] != os.environ['HERDR_PANE_ID']:
        raise HerdrError('Caller pane does not match the selected Herdr session')
    config = {'session': session, 'binary': binary, 'anchor_pane': pane['pane_id'],
              'nodes': list(WORKFLOW_NODE_NAMES), 'direction': direction, 'wait_seconds': wait_seconds}
    project.mkdir(parents=True, exist_ok=True)
    _write_json(directory(project) / 'config.json', config)
    return config


def load_config(project):
    path = directory(project) / 'config.json'
    if not path.is_file():
        return None
    config = json.loads(path.read_text())
    if (set(config) != {'session', 'binary', 'anchor_pane', 'nodes', 'direction', 'wait_seconds'}
            or config['nodes'] != list(WORKFLOW_NODE_NAMES) or config['direction'] not in {'right', 'down'}
            or type(config['wait_seconds']) not in (int, float) or not 0 < config['wait_seconds'] <= 86400):
        raise ValueError('Invalid Herdr configuration; reconnect to enable persistent role agents')
    return config


def _read(path):
    return json.loads(path.read_text()) if path.is_file() else None


def _relay(folder, offset, on_event):
    path = folder / 'events.jsonl'
    if not path.is_file():
        return offset
    with path.open('rb') as stream:
        stream.seek(offset)
        while line := stream.readline():
            if not line.endswith(b'\n'):
                break
            if on_event:
                on_event(json.loads(line))
            offset = stream.tell()
    return offset


def ensure_no_pending(project):
    active = _read(directory(project) / 'active.json')
    if active and not active.get('consumed'):
        raise HerdrError(f"Previous task {active['id']} must be reconciled with paper-herdr wait before new work")


def wait(project, task_id, *, on_event=None, wait_seconds=7200):
    """Reconcile an existing task; never resubmit commands on timeout/lost response."""
    if not re.fullmatch(r'[a-f0-9]{32}', task_id):
        raise ValueError('Invalid Herdr task ID')
    if not 0 < wait_seconds <= 86400:
        raise ValueError('Wait timeout must be positive and at most 86400 seconds')
    folder = directory(project) / 'tasks' / task_id
    request = _read(folder / 'request.json')
    if request is None or request['project'] != str(Path(project).resolve()):
        raise HerdrError('Task does not belong to this project')
    last_poll = time.monotonic()
    deadline, offset = last_poll + wait_seconds, 0
    while True:
        offset = _relay(folder, offset, on_event)
        receipt = _read(folder / 'result.json')
        if receipt:
            _relay(folder, offset, on_event)
            if receipt['id'] != task_id:
                raise HerdrError('Result identity mismatch')
            if not receipt.get('state_sha256'):
                raise HerdrError(receipt.get('error') or 'Worker ended without a workflow receipt')
            raw = (Path(project) / 'workflow-state.json').read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt['state_sha256']:
                raise HerdrError('Workflow changed after this task; inspect its receipt before continuing')
            state = WorkflowState.from_dict(json.loads(raw))
            execution = state.node_results.get(request['node'], {}).get('execution', {})
            if (execution.get('herdr_task_id') != task_id or state.current_node != request['node']
                    or receipt['status'] != state.status or state.project_goal != request['project_goal']):
                raise HerdrError('Workflow receipt does not match the submitted stage')
            if state.status in {'completed', 'awaiting_user'}:
                for ref in state.artifacts.get(request['node'], []):
                    artifact = (Path(project) / ref['path']).resolve()
                    if not artifact.is_relative_to(Path(project).resolve()) or not artifact.is_file():
                        raise HerdrError('Artifact is missing or outside the project')
                    if hashlib.sha256(artifact.read_bytes()).hexdigest() != ref['sha256']:
                        raise HerdrError('Artifact changed after execution; inspect the original receipt')
            return state
        current = time.monotonic()
        from .user_questions import UserQuestions
        started = _read(folder / 'started.json') or {}
        # Time spent waiting for the human is outside the worker execution budget.
        if any(q['live'] and q['owner_pid'] == started.get('pid')
               for q in UserQuestions(project).pending()):
            deadline += current - last_poll
        last_poll = current
        if current >= deadline:
            raise HerdrError(f'Task {task_id} outcome pending; use paper-herdr wait, do not resubmit')
        time.sleep(0.5)


def role_for_node(node):
    from .role_sessions import PHASE_ROLES
    return PHASE_ROLES['research_refine' if node == 'refine' else node]


def host_alive(ready, root):
    try:
        command = Path(f"/proc/{int(ready['pid'])}/cmdline").read_bytes().split(b'\0')
        return b'paper_agent.agent.herdr_role' in command and str(root).encode() in command
    except (OSError, KeyError, ValueError, TypeError):
        return False


def ensure_role(project, role, config, client):
    """Reuse only a verified host; lost startup responses never spawn duplicates."""
    require_managed_pane()
    if role not in {'idea_generator', 'experiment', 'paper_writer'}:
        raise ValueError('Unknown execution role')
    root = directory(project) / 'roles' / role
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'startup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('Role startup already in progress') from exc
        binding = _read(root / 'binding.json')
        if binding:
            if binding.get('session') != config['session']:
                raise HerdrError('Role belongs to another Herdr session; inspect the original host before rebinding')
            if not binding.get('pane_id'):
                raise HerdrError('Role pane creation is unresolved; inspect the session before restarting')
            pane = client.call('pane', 'get', binding['pane_id'])['pane']
            ready = _read(root / 'ready.json')
            if (ready and ready.get('generation') == binding['generation']
                    and host_alive(ready, root)
                    and pane.get('agent') == 'paper-agent-' + role
                    and pane.get('cwd') and Path(pane['cwd']).resolve() == Path(project).resolve()):
                return binding
            raise HerdrError('Role host is absent or startup is unresolved; inspect its pane before restarting')
        client.call('pane', 'get', config['anchor_pane'])
        binding = {'generation': uuid4().hex, 'role': role, 'session': config['session'],
                   'binary': config['binary'], 'project': str(Path(project).resolve()), 'status': 'starting'}
        _write_json(root / 'binding.json', binding)
        siblings = [_read(directory(project) / 'roles' / other / 'binding.json')
                    for other in ('idea_generator', 'experiment', 'paper_writer') if other != role]
        sibling = next((item for item in reversed(siblings) if item and item.get('pane_id')), None)
        response = client.call('pane', 'split', '--pane', sibling['pane_id'] if sibling else config['anchor_pane'],
                               '--direction', 'down' if sibling else config['direction'],
                               '--cwd', str(project), '--no-focus')
        binding['pane_id'] = response['pane']['pane_id']
        _write_json(root / 'binding.json', binding)
        command = shlex.join([sys.executable, '-m', 'paper_agent.agent.herdr_role', str(root)])
        client.call('pane', 'run', binding['pane_id'], command)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = _read(root / 'ready.json')
            if ready and ready.get('generation') == binding['generation']:
                binding['status'] = 'ready'
                _write_json(root / 'binding.json', binding)
                return binding
            time.sleep(0.1)
        raise HerdrError('Role startup pending; do not create a duplicate host')


def submit(project, node, instruction, *, role, project_goal, revise=None, on_event=None, **options):
    require_managed_pane()
    project = Path(project).resolve()
    config = load_config(project)
    if config is None or node not in config['nodes']:
        raise HerdrError('Stage is not enabled for Herdr')
    client = Client(config['session'], config['binary'])
    root = directory(project)
    with (root / 'transport.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('Another Herdr stage is being submitted or awaited') from exc
        ensure_no_pending(project)
        if role_for_node(node) != role:
            raise ValueError('Stage does not belong to this role')
        binding = ensure_role(project, role, config, client)
        task_id = uuid4().hex
        folder = root / 'tasks' / task_id
        request = {'id': task_id, 'project': str(project), 'node': node, 'role': role,
                   'project_goal': project_goal, 'instruction': instruction, 'revise': revise,
                   'options': {key: asdict(value) if is_dataclass(value) else value
                               for key, value in options.items()},
                   'session': config['session'], 'binary': config['binary']}
        _write_json(folder / 'request.json', request)
        active = {'id': task_id, 'delivery': 'creating_pane', 'consumed': False}
        _write_json(root / 'active.json', active)
        try:
            pane = binding['pane_id']
            active.update(pane_id=pane, delivery='submitting')
            _write_json(root / 'active.json', active)
            # Durable mailbox: never type over a person's terminal draft.
            _write_json(root / 'roles' / role / 'inbox.json', {'id': task_id})
            active['delivery'] = 'submitted'
            _write_json(root / 'active.json', active)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                (folder / 'cancel').touch()
            active['delivery'] = 'unknown'
            _write_json(root / 'active.json', active)
            raise
        try:
            if on_event:
                on_event({'kind': 'herdr_submitted', 'herdr_task_id': task_id, 'pane_id': pane,
                          'node': node, 'role': role})
            state = wait(project, task_id, on_event=on_event, wait_seconds=config['wait_seconds'])
        except (KeyboardInterrupt, SystemExit):
            # Cooperative cancellation is observed by the worker, not sent to a human shell.
            (folder / 'cancel').touch()
            raise
        active['consumed'] = True
        _write_json(root / 'active.json', active)
        return state


def reconcile(project, task_id, *, wait_seconds=7200):
    """Consume a durable result only after the submitting caller has released ownership."""
    root = directory(project)
    if not root.is_dir():
        raise HerdrError('Project has no Herdr tasks')
    with (root / 'transport.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HerdrError('The submitting caller is still awaiting this task') from exc
        state = wait(project, task_id, wait_seconds=wait_seconds)
        active = _read(root / 'active.json')
        if active and active['id'] == task_id:
            active['consumed'] = True
            _write_json(root / 'active.json', active)
        return state


def main():
    parser = argparse.ArgumentParser(description='Connect persistent Herdr role agents or reconcile tasks')
    sub = parser.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('connect')
    setup.add_argument('--project', type=Path, required=True)
    setup.add_argument('--session', required=True)
    setup.add_argument('--binary')
    setup.add_argument('--direction', choices=['right', 'down'], default='down')
    team = sub.add_parser('team')
    team.add_argument('--project', type=Path, required=True)
    waiting = sub.add_parser('wait')
    waiting.add_argument('--project', type=Path, required=True)
    waiting.add_argument('task_id')
    waiting.add_argument('--timeout', type=float, default=7200)
    status = sub.add_parser('status')
    status.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'status':
        active = _read(directory(args.project) / 'active.json')
        folder = directory(args.project) / 'tasks' / active['id'] if active else None
        print(json.dumps({'active': active,
            'started': _read(folder / 'started.json') if folder else None,
            'result': _read(folder / 'result.json') if folder else None}, ensure_ascii=False))
    elif args.action == 'connect':
        print(json.dumps(connect(args.project, args.session, args.binary, args.direction), ensure_ascii=False))
    elif args.action == 'team':
        config = load_config(args.project)
        if config is None:
            raise HerdrError('Connect the project first')
        print(json.dumps({role: ensure_role(args.project, role, config, Client(config['session'], config['binary']))
                          for role in ('idea_generator', 'experiment', 'paper_writer')}, ensure_ascii=False))
    else:
        state = reconcile(args.project, args.task_id, wait_seconds=args.timeout)
        print(json.dumps({'status': state.status, 'error': state.error}, ensure_ascii=False))


if __name__ == '__main__':
    main()
