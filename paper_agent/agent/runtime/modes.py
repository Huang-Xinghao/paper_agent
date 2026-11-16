"""Resolve collaboration mode without replacing an existing project's choice."""
from pathlib import Path

from paper_agent.storage import read_json


def resolve_mode(project, requested=None, *, default='auto'):
    state = read_json(Path(project) / 'workflow-state.json', {})
    mode = requested if requested is not None else state.get('mode', default)
    if mode not in {'auto', 'expert'}:
        raise ValueError('Unknown collaboration mode')
    return mode
