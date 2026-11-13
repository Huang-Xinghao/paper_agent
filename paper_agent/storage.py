"""Atomic JSON persistence with explicit strict and forgiving read policies."""

import json
import os
import tempfile
from pathlib import Path


def write_json(path, value):
    """Publish complete JSON atomically on the same filesystem."""
    atomic_bytes(Path(path), (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

def read_json(path, default=None):
    """Default absent records; reject unreadable or malformed existing records."""
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def read_json_or_default(path, default=None):
    """Allow status views to default unreadable or malformed records as before."""
    try:
        return read_json(path, default)
    except (ValueError, OSError):
        return default


def atomic_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
