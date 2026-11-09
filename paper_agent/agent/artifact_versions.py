"""Conservative workflow dependencies and content versions, independent of UI state."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path

from .role_sessions import now

ORDER = ('literature', 'data_search', 'ideas', 'refine', 'experiment_plan',
         'experiment_bridge', 'paper_plan', 'paper_figure', 'paper_write',
         'paper_compile', 'improvement')


def node_files(project, node):
    from .workflow import WorkflowRunner
    paths = set(WorkflowRunner._review_files(node))
    patterns = {'paper_figure': ('figures/*.pdf', 'figures/*.png', 'figures/*.svg', 'figures/*.jpg', 'figures/*.jpeg'),
                'paper_write': ('paper/sections/*.tex',)}.get(node, ())
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
            return json.loads(self.path.read_text())
        # Adopt legacy outputs once, without asserting any new scientific approval.
        nodes = {}
        state_path = self.project / 'workflow-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        for node in ORDER:
            files = hashes(self.project, node_files(self.project, node))
            if files:
                recorded = {r['path']: r['sha256'] for r in state.get('artifacts', {}).get(node, [])
                            if 'path' in r and 'sha256' in r}
                nodes[node] = {'version': 1, 'files': {**files, **recorded}, 'adopted': True}
        return {'nodes': nodes, 'stale_nodes': {}}

    def _invalidate(self, data, node, reason, *, include_self=False, changed_files=()):
        start = ORDER.index(node) + (0 if include_self else 1)
        for dependent in ORDER[start:]:
            if dependent in data['nodes']:
                data['stale_nodes'][dependent] = {'caused_by': node, 'reason': reason, 'at': now(),
                                                  'kind': 'external' if include_self else 'revision',
                                                  'changed_files': sorted(changed_files)}

    def _detect_changes(self, data, *, accepted_files=()):
        accepted_files = set(accepted_files)
        for node, record in data['nodes'].items():
            current = hashes(self.project, set(record['files']) | node_files(self.project, node))
            differences = {p for p in current.keys() | record['files'].keys()
                           if current.get(p) != record['files'].get(p)}
            if differences and not differences <= accepted_files:
                self._invalidate(data, node, '产物内容与记录版本不一致，需要重新核查或生成',
                                 include_self=True, changed_files=differences)
        return data

    def _save(self, data):
        from .workflow_cli import _write_json
        _write_json(self.path, data)

    def context(self):
        # Status queries derive invalidation without modifying project state.
        return self._detect_changes(self._read())

    def prepare(self, node, *, approval=False, accepted_files=()):
        with self.locked():
            data = self._detect_changes(self._read(), accepted_files=accepted_files if approval else ())
            if approval:
                for reviewed in ORDER[:ORDER.index(node) + 1]:
                    stale = data['stale_nodes'].get(reviewed, {})
                    if (stale.get('kind') == 'external' and stale.get('changed_files')
                            and set(stale['changed_files']) <= set(accepted_files)):
                        data['stale_nodes'].pop(reviewed, None)
            self._save(data)
            preceding = ORDER[:ORDER.index(node) + (1 if approval else 0)]
            blocked = [n for n in preceding if n in data['stale_nodes']]
            if blocked:
                raise ValueError('上游产物已变更，请先重新核查或生成：' + ', '.join(blocked))
            return data

    def record(self, node, *, regenerated=False, files=()):
        """Called only after validated publication; no-op revisions change no versions."""
        with self.locked():
            data = self._read()
            old = data['nodes'].get(node, {})
            current = hashes(self.project, node_files(self.project, node) | set(files) | set(old.get('files', {})))
            changed = current != old.get('files', {})
            if changed:
                self._invalidate(data, node, '上游产物已修订，下游需要重新核查或生成')
            data['nodes'][node] = {'version': old.get('version', 0) + int(changed),
                                  'files': current, 'updated_at': now(),
                                  'inputs': {n: r['version'] for n, r in data['nodes'].items()
                                             if ORDER.index(n) < ORDER.index(node)}}
            if regenerated:
                data['stale_nodes'].pop(node, None)
            # Later phases can legitimately update shared sources (e.g. improvement
            # edits main.tex and recompiles). Keep their previous owners in sync.
            for other, record in data['nodes'].items():
                if other != node:
                    for name in record['files'].keys() & current.keys():
                        record['files'][name] = current[name]
            self._save(data)
            return data
