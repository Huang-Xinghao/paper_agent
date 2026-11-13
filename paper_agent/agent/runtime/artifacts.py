"""Artifact hashes and advisory change tracking, independent of UI state."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path

from paper_agent.agent.runtime.sessions import now
from paper_agent.agent.stages import BY_NODE, WORKFLOW_NODE_NAMES as ORDER
from paper_agent.storage import write_json as _write_json


def node_files(project, node):
    paths = set(BY_NODE[node].review_paths)
    patterns = {'experiment_plan': ('experiments/hpo_spec.json',),
                'experiment_bridge': ('results/hpo/best_config.json',),
                'paper_figure': ('figures/*.pdf', 'figures/*.png', 'figures/*.svg', 'figures/*.jpg', 'figures/*.jpeg'),
                'paper_write': ('paper/**/*.tex', 'paper/**/*.bib')}.get(node, ())
    for pattern in patterns:
        paths.update(p.relative_to(project).as_posix() for p in project.glob(pattern) if p.is_file())
    return paths


def tracked_files(project):
    paths = set().union(*(node_files(project, node) for node in ORDER))
    ledger = project / '.paper-agent' / 'artifact-versions.json'
    if ledger.exists():
        for record in json.loads(ledger.read_text()).get('nodes', {}).values():
            paths.update(record['files'])
    return paths


def hashes(project, paths):
    result = {}
    for name in sorted(paths):
        path = (project / name).resolve()
        if path.is_relative_to(project) and path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class ArtifactVersions:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / '.paper-agent'
        self.path = self.root / 'artifact-versions.json'

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'artifact-versions.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _read(self):
        if self.path.exists():
            return {'nodes': json.loads(self.path.read_text())['nodes']}
        return {'nodes': {}}

    def _detect_changes(self, data):
        changes = {}
        for node, record in data['nodes'].items():
            current = hashes(self.project, set(record['files']) | node_files(self.project, node))
            differences = sorted(p for p in current.keys() | record['files'].keys()
                                 if current.get(p) != record['files'].get(p))
            if differences:
                changes[node] = differences
        return {**data, 'changed_files': changes}

    def _save(self, data):
        _write_json(self.path, data)

    def context(self):
        # Status queries report changes without modifying project state.
        return self._detect_changes(self._read())

    def record(self, node, *, files=()):
        """Called only after validated publication; no-op revisions change no versions."""
        with self.locked():
            data = self._read()
            old = data['nodes'].get(node, {})
            current = hashes(self.project, node_files(self.project, node) | set(files) | set(old.get('files', {})))
            changed = current != old.get('files', {})
            data['nodes'][node] = {'version': old.get('version', 0) + int(changed),
                                  'files': current, 'updated_at': now(),
                                  'inputs': {n: r['version'] for n, r in data['nodes'].items()
                                             if ORDER.index(n) < ORDER.index(node)}}
            # Later phases can legitimately update shared sources (e.g. improvement
            # edits main.tex and recompiles). Keep their previous owners in sync.
            for other, record in data['nodes'].items():
                if other != node:
                    for name in record['files'].keys() & current.keys():
                        record['files'][name] = current[name]
            self._save(data)
            return data
