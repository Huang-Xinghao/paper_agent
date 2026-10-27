"""Durable management records. Research files remain owned by the workflow."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(timezone.utc).isoformat()


def data_root():
    root = Path(
        os.environ.get("PAPER_AGENT_WEB_DATA", REPO / ".paper-agent" / "web")
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def connection():
    db = sqlite3.connect(data_root() / "management.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archived_projects (
            project_id TEXT PRIMARY KEY REFERENCES projects(id),
            archived_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            status TEXT NOT NULL, options TEXT NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT, error TEXT, pid INTEGER, pid_created REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_project
            ON runs(project_id) WHERE status IN ('queued','running','stopping');
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT,
            kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS project_events ON events(project_id, id);
        """)


def event(project_id, kind, data, run_id=None):
    with connection() as db:
        db.execute(
            "INSERT INTO events(project_id,run_id,kind,data,created_at) VALUES(?,?,?,?,?)",
            (project_id, run_id, kind, json.dumps(data, ensure_ascii=False), now()),
        )


def rows(query, values=()):
    with connection() as db:
        return [dict(row) for row in db.execute(query, values).fetchall()]


def run_update(run_id, **values):
    with connection() as db:
        db.execute(
            "UPDATE runs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), run_id),
        )


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return default
