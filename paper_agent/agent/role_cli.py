"""CLI and main-agent interface to project-owned role records and assignments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys

from paper_agent.agent.runtime.sessions import ROLE_DESCRIPTIONS, RoleSession


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    root = CommandParser(prog='paper-roles', description=__doc__, add_help=False)
    actions = root.add_subparsers(dest='action', required=True)
    for action in ('list', 'status', 'history', 'delegate', 'revise'):
        command = actions.add_parser(action, add_help=False)
        command.add_argument('--project', type=Path)
        if action in {'status', 'history'}:
            command.add_argument('--role', choices=tuple(ROLE_DESCRIPTIONS), required=action == 'history')
        if action == 'history':
            command.add_argument('--channel', choices=('execution', 'discussion'), default='execution')
            command.add_argument('--archive', help='Archive filename returned by status')
            command.add_argument('--offset', type=int, default=0, help='Message index')
            command.add_argument('--char-offset', type=int, default=0)
            command.add_argument('--limit', type=int, default=20)
            command.add_argument('--max-chars', type=int, default=12000)
        if action in {'delegate', 'revise'}:
            command.add_argument('--node', required=True)
            command.add_argument('--instruction', required=True)
        if action == 'delegate':
            command.add_argument('--role', required=True)
        if action == 'revise':
            command.add_argument('--target', required=True)
            command.add_argument('--file', action='append', required=True)
            command.add_argument('--compile-pdf', action='store_true')
    return root


def _local(project, path):
    if not path.resolve().is_relative_to(project):
        raise ValueError('Role record resolves outside the selected project')
    return path


def _read(project, path):
    path = _local(project, path)
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def _session(project, role, channel):
    session = RoleSession(project, role, channel=channel)
    data = _read(project, session.path)
    if data is not None and data.get('role') != role:
        raise ValueError('Role session identity mismatch')
    return session, data


def _archives(project, session):
    folder = _local(project, session.folder / 'archives' / session.role)
    return sorted((p.name for p in folder.glob('*.json') if p.is_file()), reverse=True)


def status(project, role=None):
    """Read durable snapshots without taking writer locks or creating sessions."""
    records = []
    for name in (role,) if role else ROLE_DESCRIPTIONS:
        channels = {}
        for channel in ('execution', 'discussion'):
            session, data = _session(project, name, channel)
            tasks = (data or {}).get('tasks', [])
            channels[channel] = {
                'exists': data is not None, 'session_id': (data or {}).get('session_id'),
                'updated_at': (data or {}).get('updated_at'),
                'message_count': len((data or {}).get('messages', [])),
                'latest_task': {key: tasks[-1].get(key) for key in
                                ('id', 'phase', 'status', 'started_at', 'ended_at')} if tasks else None,
                'archives': _archives(project, session),
            }
        host = project / '.paper-agent/herdr/roles' / name
        binding = _read(project, host / 'binding.json') or {}
        records.append({'role': name, 'channels': channels,
                        'host': {key: binding.get(key) for key in ('session', 'pane_id', 'generation')},
                        'ready_record': _read(project, host / 'ready.json')})
    workflow = _read(project, project / 'workflow-state.json') or {}
    active = _read(project, project / '.paper-agent/herdr/active.json')
    return {'project': str(project), 'roles': records, 'active': active,
            'workflow': {key: workflow.get(key) for key in ('status', 'current_node', 'failed_node')},
            'observation': 'Persisted project and role state.'}


def _visible(value):
    """Keep transcript text/tool calls while omitting historical pixel payloads."""
    if isinstance(value, list):
        return [_visible(item) for item in value]
    if isinstance(value, dict):
        if value.get('type') in {'image_url', 'input_image', 'image'}:
            return {'type': 'text', 'text': '[Historical image payload omitted; inspect the original artifact for pixels.]'}
        return {key: _visible(item) for key, item in value.items()}
    return value


def history(project, args):
    if (args.offset < 0 or args.char_offset < 0 or not 1 <= args.limit <= 100
            or not 1 <= args.max_chars <= 20000):
        raise ValueError('Invalid history cursor or page size')
    session = RoleSession(project, args.role, channel=args.channel)
    path = session.path
    if args.archive:
        if Path(args.archive).name != args.archive or args.archive not in _archives(project, session):
            raise ValueError('Choose an archive filename returned by status')
        path = session.folder / 'archives' / args.role / args.archive
    _local(project, path)
    if not path.is_file():
        return {'role': args.role, 'channel': args.channel, 'exists': False,
                'messages': [], 'next_offset': None, 'next_char_offset': None}
    raw = path.read_bytes()
    data = json.loads(raw)
    if isinstance(data, dict) and data.get('role') != args.role:
        raise ValueError('Role session identity mismatch')
    messages = data if isinstance(data, list) else data['messages']
    result, remaining = [], args.max_chars
    index, char_offset = args.offset, args.char_offset
    if index > len(messages) or index == len(messages) and char_offset:
        raise ValueError('History cursor is past the end')
    while index < len(messages) and len(result) < args.limit and remaining:
        message = messages[index]
        visible = _visible({key: value for key, value in message.items() if key != 'role'})
        text = json.dumps(visible, ensure_ascii=False)
        if char_offset > len(text):
            raise ValueError('Character cursor is past the end of this message')
        chunk = text[char_offset:char_offset + remaining]
        result.append({'index': index, 'role': message.get('role'), 'char_offset': char_offset,
                       'text': chunk, 'partial': char_offset > 0 or char_offset + len(chunk) < len(text)})
        remaining -= len(chunk)
        char_offset += len(chunk)
        if char_offset < len(text):
            break
        index, char_offset = index + 1, 0
    return {'project': str(project), 'role': args.role, 'channel': args.channel, 'exists': True,
            'archive': args.archive, 'total_messages': len(messages), 'messages': result,
            'next_offset': index if index < len(messages) else None,
            'next_char_offset': char_offset if index < len(messages) else None,
            'reading_policy': 'Discussion and execution have separate histories. Archives contain pre-compaction messages.'}


def parse_command(command):
    words = shlex.split(command) if isinstance(command, str) else list(command)
    if words[:1] == ['paper-roles']:
        words = words[1:]
    return parser().parse_args(words)


def run(command, *, project=None, delegate=None, revise=None):
    """Run the same command grammar in a terminal or a project-bound main tool."""
    args = parse_command(command)
    selected = (args.project or project)
    if selected is None:
        raise ValueError('--project is required')
    selected = Path(selected).expanduser().resolve()
    if project is not None and selected != Path(project).resolve():
        raise ValueError('Main can only control roles in its own project')
    if not selected.is_dir():
        raise ValueError('Project directory does not exist')
    if args.action in {'list', 'status'}:
        return status(selected, getattr(args, 'role', None))
    if args.action == 'history':
        return history(selected, args)
    from paper_agent.agent.runtime.execution import StageAssignment, RevisionTask
    if args.action == 'delegate':
        task = StageAssignment(node=args.node, role=args.role, instruction=args.instruction)
        if delegate is not None:
            return delegate(task.model_dump())
    else:
        task = RevisionTask(node=args.node, target=args.target, instruction=args.instruction,
                            files=args.file, compile_pdf=args.compile_pdf)
        if revise is not None:
            return revise(task.model_dump())
    # The task is already explicit; use the main's dispatcher without model planning.
    from .coordinator import CoordinatorAgent
    return json.loads(CoordinatorAgent(selected, max_assignments=1).dispatch(task).output)


def main():
    if sys.argv[1:] in (['--help'], ['-h']):
        parser().print_help()
        return
    try:
        result = run(sys.argv[1:])
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))
    if result.get('status') in {'failed', 'interrupted'}:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
