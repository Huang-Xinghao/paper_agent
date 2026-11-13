"""Task contracts, dispatch, durable outcomes and cancellation ownership."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .sessions import now
from paper_agent.storage import write_json as _write_json
from paper_agent.storage import read_json_or_default as read_json


Node = Literal['literature', 'data_search', 'ideas', 'refine', 'experiment_plan',
               'experiment_bridge', 'paper_plan', 'paper_figure', 'paper_write',
               'paper_compile', 'improvement']
Role = Literal['idea_generator', 'experiment', 'paper_writer']


class StageAssignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node: Node
    instruction: str = Field(min_length=1, max_length=20000)
    role: Role

    @model_validator(mode='after')
    def nonempty(self):
        if not self.instruction.strip():
            raise ValueError('Assignment must have a concrete instruction')
        return self


class RevisionTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node: Node
    target: str = Field(min_length=1, max_length=1000)
    instruction: str = Field(min_length=1, max_length=20000)
    files: list[str] = Field(min_length=1, max_length=40)
    compile_pdf: bool = False

    @model_validator(mode='after')
    def paths(self):
        if self.node == 'experiment_bridge':
            raise ValueError('Experiment execution cannot be a scoped artifact revision')
        if not self.instruction.strip() or not self.target.strip():
            raise ValueError('Revision must have a concrete target and instruction')
        for name in self.files:
            p = Path(name)
            if (p.is_absolute() or '..' in p.parts or not p.parts or
                    any(part.startswith('.') or part in {'audit', 'backup', 'backups'} for part in p.parts) or
                    p.parts[0] in {'results', 'experiment-runs', 'papers'} or
                    p.name.startswith(('workflow-', 'experiment-queue', 'project-config')) or
                    p.suffix.lower() not in {'.py', '.tex', '.bib', '.md', '.png', '.pdf', '.svg', '.jpg', '.jpeg'}):
                raise ValueError(f'不允许修改此路径：{name}')
        self.files = list(dict.fromkeys(self.files))
        return self


class ExecutionTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    project: str
    node: Node
    role: Role
    instruction: str = Field(min_length=1)
    project_goal: str
    kind: Literal['stage', 'revision'] = 'stage'
    revision_task: RevisionTask | None = None
    revise: str | None = None
    options: dict = Field(default_factory=dict)
    session: str | None = None
    binary: str | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.kind == 'revision') != (self.revision_task is not None):
            raise ValueError('Revision execution requires exactly one revision task')
        if self.revision_task and (self.revision_task.node != self.node or self.revise is not None):
            raise ValueError('Revision node or checkpoint mode does not match the task')
        return self


OWNER_ENV = 'PAPER_AGENT_EXECUTION_OWNER'


class ExecutionOwner:
    def __init__(self, project, folder):
        self.project = Path(project).resolve()
        self.folder = Path(folder).resolve()

    @contextmanager
    def locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / 'ownership.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def register(self, task_id):
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('Invalid execution task ID')
        with self.locked():
            _write_json(self.folder / 'execution-tasks' / (task_id + '.json'),
                        {'id': task_id, 'project': str(self.project)})
            if self.cancelled():
                self._notify(task_id)

    def cancelled(self):
        return (self.folder / 'cancel').exists() or (self.folder / 'stop').exists()

    def _ids(self):
        from .herdr import _read
        return [path.stem for path in (self.folder / 'execution-tasks').glob('*.json')
                if re.fullmatch(r'[a-f0-9]{32}', path.stem)
                and (_read(path) or {}).get('project') == str(self.project)]

    def _notify(self, task_id):
        from .herdr import directory
        target = directory(self.project) / 'tasks' / task_id
        target.mkdir(parents=True, exist_ok=True)
        (target / 'cancel').touch()

    def cancel(self):
        # Registration and cancellation serialize, including dispatch after stop.
        with self.locked():
            (self.folder / 'cancel').touch()
            for task_id in self._ids():
                self._notify(task_id)

    def settle(self, *, wait_seconds=30):
        """Validate the owned active receipt; missing outcomes keep the guard set."""
        from .herdr import directory, _read, reconcile
        deadline = time.monotonic() + wait_seconds
        while True:
            active = _read(directory(self.project) / 'active.json')
            if not active or active.get('consumed') or active.get('id') not in self._ids():
                return None
            try:
                if not (directory(self.project) / 'tasks' / active['id'] / 'result.json').exists():
                    raise RuntimeError('Task ' + active['id'] + ' outcome pending; inspect its original receipt')
                reconcile(self.project, active['id'], wait_seconds=.01)
                return None
            except Exception as exc:
                if time.monotonic() >= deadline:
                    return '角色任务取消结果仍需核查，不会自动重跑：' + str(exc)
                time.sleep(.1)


def register_execution(project, task_id):
    folder = os.environ.get(OWNER_ENV)
    if folder:
        ExecutionOwner(project, folder).register(task_id)


def new_task(project, node, instruction, *, role, project_goal, kind='stage',
             revision_task=None, revise=None, **options):
    return ExecutionTask(
        id=uuid4().hex, project=str(Path(project).resolve()), node=node, role=role,
        instruction=instruction, project_goal=project_goal, kind=kind,
        revision_task=revision_task, revise=revise,
        options={key: asdict(value) if is_dataclass(value) else value for key, value in options.items()})


def execute_task(folder, *, on_event=None, execution=None):
    from paper_agent.agent.workflow_cli import execute_stage, refresh_revision_checkpoint
    from paper_agent.agent.workflow import WorkflowConfig, IdeaNodeConfig
    folder = Path(folder)
    task = ExecutionTask.model_validate_json((folder / 'request.json').read_text())
    project = Path(task.project).resolve()
    with (folder / 'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / 'result.json').exists() or (folder / 'started.json').exists():
            raise RuntimeError('Task was already started; inspect its receipt instead of replaying it')
        _write_json(folder / 'started.json', {'id': task.id, 'pid': os.getpid(), **(execution or {})})
        metadata = {'backend': 'local', 'execution_task_id': task.id, **(execution or {})}
        events_path = folder / 'events.jsonl'

        def emit(record):
            if record['kind'] != 'execution_end' and (folder / 'cancel').exists():
                raise KeyboardInterrupt('Task cancelled')
            record = {**record, 'execution_task_id': task.id,
                      **({'herdr_task_id': task.id} if metadata['backend'] == 'herdr' else {})}
            with events_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            if on_event:
                on_event(record)

        files, final, state = [], '', None
        try:
            emit({'kind': 'execution_start', 'time': now(), 'node': task.node,
                  'role': task.role, 'task_kind': task.kind})
            if (folder / 'cancel').exists():
                raise KeyboardInterrupt('Task cancelled before execution')
            options = task.options.copy()
            if task.kind == 'stage':
                for key, cls in (('config', WorkflowConfig), ('idea_config', IdeaNodeConfig)):
                    if isinstance(options.get(key), dict):
                        options[key] = cls(**options[key])
                state = execute_stage(project, task.node, task.instruction,
                                      role=task.role, project_goal=task.project_goal,
                                      revise=task.revise, execution=metadata, on_event=emit, **options)
                status, error = state.status, state.error
                files = [ref['path'] for ref in state.artifacts.get(task.node, [])]
            else:
                from paper_agent.agent.runtime.revision import run_node_task
                if task.node == 'refine':
                    from paper_agent.proposal_review import require_resolved_proposal_review
                    require_resolved_proposal_review(project)
                result = run_node_task(project, task.revision_task.model_dump(), task.instruction,
                                       folder, on_event=emit, **options)
                status, error = result.status, result.error
                final, files = getattr(result, 'final', ''), getattr(result, 'files', [])
                if status == 'completed' and refresh_revision_checkpoint(project, task.node, task.project_goal):
                    status = 'awaiting_user'
        except BaseException as exc:
            if not isinstance(exc, (Exception, KeyboardInterrupt, SystemExit)):
                raise
            status = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
            error = str(exc) or 'Task interrupted'
        # Transport records outcomes. Only stage/checkpoint services write workflow state.
        if state is None and (project / 'workflow-state.json').is_file():
            try:
                state_data = json.loads((project / 'workflow-state.json').read_text())
            except (ValueError, OSError):
                state_data = None
        else:
            state_data = state.to_dict() if state is not None else None
        if task.kind == 'stage' and state_data is not None and status in {'failed', 'interrupted'}:
            state_data = {**state_data, 'status': status, 'error': error,
                          'current_node': task.node, 'failed_node': task.node}
        snapshot_hash = None
        if state_data is not None:
            _write_json(folder / 'workflow.json', state_data)
            snapshot_hash = hashlib.sha256((folder / 'workflow.json').read_bytes()).hexdigest()
        artifacts = []
        try:
            if status in {'completed', 'awaiting_user'}:
                artifacts = _artifact_receipts(project, files)
                if task.kind == 'stage':
                    expected = {ref['path']: ref['sha256']
                                for ref in state.artifacts.get(task.node, [])}
                    if any(ref['sha256'] != expected[ref['path']] for ref in artifacts):
                        raise ValueError('Artifact changed after workflow validation')
        except (ValueError, OSError) as exc:
            status, error = 'failed', str(exc)
        receipt = {'id': task.id, 'kind': task.kind, 'node': task.node, 'role': task.role,
                   'status': status, 'error': error, 'final': final, 'artifacts': artifacts,
                   'execution': metadata, 'ended_at': now(), 'state_sha256': snapshot_hash,
                   'workflow_snapshot': 'workflow.json' if state_data is not None else None}
        _write_json(folder / 'result.json', receipt)
        emit({'kind': 'execution_end', 'time': now(), 'node': task.node, 'role': task.role,
              'task_kind': task.kind, 'status': status, 'error': error})
        from paper_agent.agent.workflow import WorkflowState
        snapshot = WorkflowState.from_dict(state_data) if state_data is not None else None
        return _result(task, receipt, snapshot)


def _artifact_receipts(project, files):
    artifacts = []
    for name in sorted(set(files)):
        path = (project / name).resolve()
        if not path.is_relative_to(project) or not path.is_file():
            raise ValueError('Published artifact is missing or outside project')
        artifacts.append({'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return artifacts


def read_result(folder, *, verify_artifacts=True):
    """Read an immutable task snapshot, independent of the live workflow cursor."""
    from paper_agent.agent.workflow import WorkflowState
    folder = Path(folder)
    receipt = json.loads((folder / 'result.json').read_text())
    if 'workflow_snapshot' not in receipt:
        raise ValueError('Execution receipt is missing its required workflow_snapshot field')
    task = ExecutionTask.model_validate_json((folder / 'request.json').read_text())
    if any(receipt.get(key) != getattr(task, key) for key in ('id', 'kind', 'node', 'role')):
        raise ValueError('Result identity mismatch')
    if receipt.get('execution', {}).get('execution_task_id') != task.id:
        raise ValueError('Execution identity mismatch')
    snapshot = None
    if receipt['workflow_snapshot'] is not None:
        if receipt['workflow_snapshot'] != 'workflow.json':
            raise ValueError('Invalid workflow snapshot path')
        raw = (folder / 'workflow.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt['state_sha256']:
            raise ValueError('Workflow snapshot changed after execution')
        snapshot = WorkflowState.from_dict(json.loads(raw))
    if verify_artifacts:
        project = Path(task.project).resolve()
        for ref in receipt['artifacts']:
            artifact = (project / ref['path']).resolve()
            if not artifact.is_relative_to(project) or not artifact.is_file():
                raise ValueError('Artifact is missing or outside the project')
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != ref['sha256']:
                raise ValueError('Artifact changed after execution; inspect the original receipt')
    if task.kind == 'stage':
        if snapshot is None:
            snapshot = WorkflowState(task.id, project_goal=task.project_goal, current_node=task.node)
        if receipt['status'] in {'completed', 'awaiting_user', 'awaiting_results'}:
            executed = snapshot.node_results.get(task.node, {}).get('execution', {})
            if (executed.get('execution_task_id') != task.id or snapshot.project_goal != task.project_goal
                    or snapshot.status != receipt['status']):
                raise ValueError('Workflow snapshot does not match the submitted stage')
    return _result(task, receipt, snapshot)


def _result(task, receipt, snapshot):
    """Build a return value from the in-memory result or a checked disk receipt."""
    if task.kind == 'stage':
        if snapshot is None:
            from paper_agent.agent.workflow import WorkflowState
            snapshot = WorkflowState(task.id, project_goal=task.project_goal, current_node=task.node)
        snapshot.status, snapshot.error = receipt['status'], receipt['error']
        if snapshot.status in {'failed', 'interrupted'}:
            snapshot.failed_node = snapshot.current_node = task.node
        return snapshot
    return SimpleNamespace(status=receipt['status'], error=receipt['error'], final=receipt['final'],
                           files=[r['path'] for r in receipt['artifacts']],
                           task_id=task.id, execution=receipt['execution'])


@contextmanager
def project_write_lock(project):
    path = Path(project) / '.web-run.lock'
    inherited = os.environ.get('PAPER_AGENT_PROJECT_LOCK_FD')
    if inherited:
        fd = int(inherited)
        held, expected = os.fstat(fd), path.stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError('Inherited project lock does not match this project')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    else:
        with path.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def dispatch_stage(project, node, instruction, *, on_event=None, **options):
    from paper_agent.agent.runtime.herdr import connect, ensure_no_pending, load_config, submit, directory
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    ensure_no_pending(project)
    config = load_config(project)
    session = os.environ.get('PAPER_AGENT_HERDR_SESSION')
    if config is None and session:
        config = connect(project, session)
    with project_write_lock(project):
        if config:
            return submit(project, node, instruction, on_event=on_event, **options)
        root = directory(project)
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'transport.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ensure_no_pending(project)
            task = new_task(project, node, instruction, **options)
            folder = root / 'tasks' / task.id
            _write_json(folder / 'request.json', task.model_dump())
            active = {'id': task.id, 'backend': 'local', 'delivery': 'submitted', 'consumed': False}
            _write_json(root / 'active.json', active)
            register_execution(project, task.id)
            result = execute_task(folder, on_event=on_event)
            active['consumed'] = True
            _write_json(root / 'active.json', active)
            return result


def dispatch_revision(project, revision_task, instruction, *, project_goal=None, **options):
    from paper_agent.agent.runtime.sessions import revision_role
    task = RevisionTask.model_validate(revision_task)
    goal = project_goal or read_json(Path(project) / 'workflow-state.json', {}).get('project_goal', '')
    return dispatch_stage(project, task.node, instruction, role=revision_role(Path(project), task.node),
                     project_goal=goal, kind='revision', revision_task=task, **options)
