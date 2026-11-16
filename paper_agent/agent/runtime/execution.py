"""Task contracts, dispatch, durable outcomes and cancellation ownership."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import fcntl
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
Role = Literal['idea_generator', 'experiment', 'paper_writer', 'wiki_agent']


class StageAssignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node: Node
    instruction: str = Field(min_length=1, max_length=20000)
    role: Role

    @model_validator(mode='after')
    def nonempty(self):
        if self.role == 'wiki_agent':
            raise ValueError('Use the standalone Wiki task for wiki_agent')
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
    node: Node | Literal['wiki']
    role: Role
    instruction: str = Field(min_length=1)
    project_goal: str
    kind: Literal['stage', 'revision', 'wiki'] = 'stage'
    revision_task: RevisionTask | None = None
    revise: str | None = None
    options: dict = Field(default_factory=dict)
    session: str | None = None
    binary: str | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.kind == 'wiki') != (self.node == 'wiki') or (self.role == 'wiki_agent') != (self.kind == 'wiki'):
            raise ValueError('Wiki execution requires kind=wiki, node=wiki and role=wiki_agent')
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
                task_folder = directory(self.project) / 'tasks' / active['id']
                if not (task_folder / 'result.json').exists():
                    recover_cancelled_local_task(task_folder)
                if not (task_folder / 'result.json').exists():
                    raise RuntimeError('Task ' + active['id'] + ' outcome pending; inspect its original receipt')
                reconcile(self.project, active['id'], wait_seconds=.01)
                return None
            except Exception as exc:
                if time.monotonic() >= deadline:
                    return '角色任务取消结果仍需核查，不会自动重跑：' + str(exc)
                time.sleep(.1)


def recover_orphaned_task(folder):
    """Close a lost outcome only after the worker releases its execution lock."""
    folder = Path(folder)
    if not (folder / 'started.json').exists():
        return False
    with (folder / 'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if (folder / 'result.json').exists():
            return False
        started = read_json(folder / 'started.json', {})
        task = ExecutionTask.model_validate_json((folder / 'request.json').read_text())
        # A late mailbox delivery must not restart the closed task.
        (folder / 'cancel').touch()
        backend = started.get('backend', 'local')
        error = 'Worker released its execution lock without saving a result; continue using existing artifacts'
        root = Path(task.project) / '.paper-agent/herdr'
        inbox = root / 'roles' / task.role / 'inbox.json'
        if read_json(inbox, {}).get('id') == task.id:
            inbox.unlink(missing_ok=True)
        _write_json(folder / 'result.json', {
            'id': task.id, 'kind': task.kind, 'node': task.node, 'role': task.role,
            'status': 'interrupted', 'error': error, 'final': '', 'artifacts': [],
            'execution': {key: value for key, value in {
                'backend': backend, 'execution_task_id': task.id,
                'herdr_task_id': started.get('herdr_task_id'),
                'pane_id': started.get('pane_id'),
            }.items() if value is not None},
            'ended_at': now(),
        })
        return True


# Compatibility for callers using the previous local-only recovery helper.
def recover_cancelled_local_task(folder):
    return recover_orphaned_task(folder)


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

        files, final, state, wiki_result = [], '', None, None
        try:
            emit({'kind': 'execution_start', 'time': now(), 'node': task.node,
                  'role': task.role, 'task_kind': task.kind})
            if (folder / 'cancel').exists():
                raise KeyboardInterrupt('Task cancelled before execution')
            options = task.options.copy()
            if task.kind == 'stage':
                for key, cls in (('config', WorkflowConfig), ('idea_config', IdeaNodeConfig)):
                    if isinstance(options.get(key), dict):
                        if key == 'config':
                            options[key].pop('max_experiment_repairs', None)
                        options[key] = cls(**options[key])
                state = execute_stage(project, task.node, task.instruction,
                                      role=task.role, project_goal=task.project_goal,
                                      revise=task.revise, execution=metadata, on_event=emit, **options)
                status, error = state.status, state.error
                files = [ref['path'] for ref in state.artifacts.get(task.node, [])]
            elif task.kind == 'wiki':
                from paper_agent.agent.wiki_role import run_wiki_role
                wiki_result = run_wiki_role(
                    project, task.instruction,
                    on_progress=lambda kind, details: emit({'kind': kind, 'time': now(), **details}),
                    **options)
                status = 'completed' if wiki_result['status'] in {'completed', 'skipped'} else 'failed'
                error = wiki_result.get('error') if status == 'failed' else None
                final, files = wiki_result['final'], [wiki_result['receipt_path']]
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
        # Keep only this task's outcome; workflow-state.json owns project state.
        stage_result = None
        if state is not None:
            stage_result = {
                'node_results': {task.node: state.node_results.get(task.node, {})},
                'expert_checkpoint': state.expert_checkpoint,
            }
        result = {'id': task.id, 'kind': task.kind, 'node': task.node, 'role': task.role,
                  'status': status, 'error': error, 'final': final,
                  'artifacts': [{'path': name} for name in sorted(set(files))],
                  'execution': metadata, 'ended_at': now(), 'stage_result': stage_result}
        if task.kind == 'wiki':
            result['wiki_result'] = wiki_result
        _write_json(folder / 'result.json', result)
        emit({'kind': 'execution_end', 'time': now(), 'node': task.node, 'role': task.role,
              'task_kind': task.kind, 'status': status, 'error': error})
        return _result(task, result)


def read_result(folder, *, verify_artifacts=True):
    """Read a task outcome. Later edits to project files do not invalidate it."""
    folder = Path(folder)
    result = json.loads((folder / 'result.json').read_text())
    task = ExecutionTask.model_validate_json((folder / 'request.json').read_text())
    if result.get('id') != task.id:
        raise ValueError('Result belongs to another task')
    # Read old results during migration, without revalidating old file hashes.
    if 'stage_result' not in result and (folder / 'workflow.json').is_file():
        old = json.loads((folder / 'workflow.json').read_text())
        result['stage_result'] = {
            'node_results': {task.node: old.get('node_results', {}).get(task.node, {})},
            'expert_checkpoint': old.get('expert_checkpoint'),
        }
    return _result(task, result)


def _result(task, result):
    if task.kind == 'stage':
        from paper_agent.agent.workflow import WorkflowState
        state = WorkflowState(task.id, project_goal=task.project_goal, current_node=task.node,
                              **(result.get('stage_result') or {}))
        state.status, state.error = result['status'], result.get('error')
        if state.status in {'failed', 'interrupted'}:
            state.failed_node = task.node
        return state
    return SimpleNamespace(status=result['status'], error=result.get('error'), final=result.get('final', ''),
                           files=[r['path'] for r in result.get('artifacts', [])],
                           task_id=task.id, execution=result.get('execution', {}),
                           wiki_result=result.get('wiki_result'))


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
