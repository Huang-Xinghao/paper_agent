"""Public execution snapshots for the project's persistent coordinator and execution roles."""
from pathlib import Path
import re

from paper_agent.agent.runtime.herdr import directory
from paper_agent.agent.runtime.sessions import ROLE_DESCRIPTIONS, RoleSession
from paper_agent.agent.stages import BY_NODE, PHASE_ROLES
from paper_agent.storage import read_json


def active_task(project):
    root = directory(project)
    active = read_json(root / 'active.json', {})
    if active.get('consumed', True) or not re.fullmatch(r'[a-f0-9]{32}', str(active.get('id', ''))):
        return None
    path = root / 'tasks' / active['id'] / 'request.json'
    if not path.resolve().is_relative_to(project):
        return None
    task = read_json(path, {})
    if (task.get('id') == active['id'] and task.get('project') == str(project)
            and task.get('role') in ROLE_DESCRIPTIONS):
        return task
    return None


def event_role(kind, data):
    if kind == 'assignment_start':
        return data.get('assignment', {}).get('role')
    if kind == 'assignment_end':
        return 'main'
    if data.get('role') in ROLE_DESCRIPTIONS:
        return data['role']
    phase = data.get('phase') or data.get('node')
    if phase == 'coordinator':
        return 'main'
    return PHASE_ROLES.get(phase) or (BY_NODE[phase].role if phase in BY_NODE else None)


def agent_views(project, status, state, questions, last_event=None):
    project = Path(project).resolve()
    running = status in {'queued', 'running', 'stopping', 'external_running'}
    execution = active_task(project) if running else None
    actor = event_role(last_event['kind'], last_event['data']) if last_event else None
    if execution:
        actor = execution['role']
    if running and actor is None:
        actor = 'main'
    waiting = {q['role'] for q in questions}
    result = []
    for role in ROLE_DESCRIPTIONS:
        session = RoleSession(project, role)
        if not session.path.resolve().is_relative_to(project):
            raise ValueError('Role session resolves outside project')
        data = read_json(session.path, {})
        task = (data.get('tasks') or [{}])[-1]
        if execution and role == actor:
            task = {**task, 'id': execution['id'], 'phase': execution['node'],
                    'instruction': execution['instruction']}
        value = dict(role=role, status=task.get('status', 'idle'),
                     phase=task.get('phase', 'coordinator' if role == 'main' else None),
                     task_id=task.get('id'), instruction=task.get('instruction', ''),
                     updated_at=data.get('updated_at'), error=task.get('error'),
                     is_active=running and actor == role)
        if running and actor == role:
            value['status'] = 'stopping' if status == 'stopping' else 'running'
        elif running and role == 'main':
            value['status'] = 'waiting'
        elif value['status'] in {'running', 'awaiting_user'} and not task.get('ended_at'):
            value['status'] = 'interrupted' if not running else 'waiting'
        if role in waiting or (role == 'main' and not running and state.get('status') == 'awaiting_user'):
            value['status'] = 'awaiting_user'
        result.append(value)
    return result
