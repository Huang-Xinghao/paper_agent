"""Append-only usage records, without prompts or credentials."""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def record_usage(workspace, *, source, model, usage=None, phase=None):
    if workspace is None:
        return
    if hasattr(usage, 'model_dump'):
        usage = usage.model_dump()
    event = dict(event_id=uuid.uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                 source=source, model=model, phase=phase,
                 usage=usage if isinstance(usage, dict) else None)
    try:
        path = Path(workspace) / '.paper-agent' / 'usage.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        # The workflow can run as root in Docker; the host collector must read this
        # credential-free ledger through the workspace bind mount.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(event) + '\n').encode())
        finally:
            os.close(fd)
    except OSError:
        logging.getLogger(__name__).warning('Could not persist usage', exc_info=True)
