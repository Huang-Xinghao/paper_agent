"""Small live status snapshots shared by tools, standalone CLIs and the web API."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import threading
import time

from paper_agent.storage import read_json_or_default, write_json


class OperationProgress:
    interval = 1.0

    def __init__(self, project, kind, target, *, role, on_progress=None, tick=None):
        key = hashlib.sha256(f'{kind}:{target}'.encode()).hexdigest()[:16]
        self.path = Path(project) / '.paper-agent/operations' / f'{key}.json'
        self.callback, self.tick = on_progress, tick
        self.started = time.monotonic()
        self.last_activity = self.started
        self.lock = threading.RLock()
        self.data = dict(operation=kind, target=str(target), role=role, pid=os.getpid(),
                         status='running', stage='preparing',
                         started_at=datetime.now(timezone.utc).isoformat())

    def update(self, stage=None, **details):
        with self.lock:
            if stage is not None:
                self.data['stage'] = stage
                self.last_activity = time.monotonic()
            self.data.update(details)
            self.data.update(elapsed_seconds=round(time.monotonic() - self.started, 1),
                             activity_age_seconds=round(time.monotonic() - self.last_activity, 1),
                             updated_at=datetime.now(timezone.utc).isoformat())
            write_json(self.path, self.data)
            if self.callback:
                self.callback('operation_progress', dict(self.data))

    @contextmanager
    def running(self):
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(self.interval):
                with self.lock:
                    if self.tick:
                        self.tick()
                    self.update()

        self.update()
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield self
        except BaseException:
            self.update(status='interrupted')
            raise
        else:
            self.update(status='completed' if self.data['status'] == 'running' else self.data['status'])
        finally:
            stopped.set()
            worker.join(timeout=2)


def live_operations(project):
    result = []
    for path in (Path(project) / '.paper-agent/operations').glob('*.json'):
        value = read_json_or_default(path, {})
        if value.get('status') != 'running':
            continue
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['updated_at'])).total_seconds()
            if age > 15:
                continue
            os.kill(value['pid'], 0)
        except (KeyError, OSError, ValueError, TypeError):
            continue
        result.append(value)
    return sorted(result, key=lambda value: value['updated_at'])
