"""Project-owned role conversations and explicit phase assignments."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from batteryscientist.agent.stages import PHASE_ROLES, ROLE_DESCRIPTIONS
from batteryscientist.agent.context import history_digest, memory_matches, prepare_history


@dataclass(frozen=True)
class PhaseTask:
    phase: str
    instruction: str
    role: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        if self.phase not in PHASE_ROLES:
            raise ValueError(f"Unknown phase: {self.phase}")
        if not self.instruction.strip() or not self.id.strip():
            raise ValueError("Task instruction and id must not be empty")
        if self.role is None:
            object.__setattr__(self, "role", PHASE_ROLES[self.phase])
        if self.role not in ROLE_DESCRIPTIONS:
            raise ValueError(f"Unknown role: {self.role}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionTask:
    session: RoleSession
    data: dict
    record: dict
    history: list[dict]
    recovered_results: list[dict]


class RoleSession:
    def __init__(self, project: Path, role: str, *, channel: str = "execution"):
        if role not in ROLE_DESCRIPTIONS:
            raise ValueError(f"Unknown role: {role}")
        self.role = role
        from batteryscientist.agent.image_store import ImageStore
        self.images = ImageStore(project)
        self.folder = project / ".batteryscientist" / "role-sessions"
        if channel not in {"execution", "discussion"}:
            raise ValueError("Unknown conversation channel")
        self.channel = channel
        if channel == "discussion":
            self.folder /= "discussions"
        self.path = self.folder / f"{role}.json"

    @contextmanager
    def locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / f"{self.role}.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Role session is already running: {self.role}") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def load(self) -> dict:
        if not self.path.exists():
            return {"session_id": uuid4().hex, "role": self.role, "channel": self.channel,
                    "messages": [], "tasks": []}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value["role"] != self.role or value.get("channel") != self.channel:
            raise ValueError("Role session identity mismatch")
        # Preserve valid summary coverage when migrating an older inline transcript.
        memory = value.get('context_memory') or {}
        covered = memory_matches(value['messages'], memory)
        self.images.externalize(value.get('messages', []))
        if covered:
            memory['prefix_sha256'] = history_digest(value['messages'][:memory['cutoff']])
        return value

    def save(self, value: dict):
        self.images.externalize(value.get('messages', []))
        value["updated_at"] = now()
        from batteryscientist.storage import write_json
        write_json(self.path, value)

    def save_conversation(self, data: dict, task: dict, state):
        """Persist the transcript and its memory using this session's identity."""
        state.role, state.session_id, state.task_id = self.role, data['session_id'], task['id']
        data['messages'] = [m for m in state.messages if m['role'] != 'system']
        data['context_memory'] = state.context_memory
        task['iterations'] = state.iterations
        task['files'] = list(state.files)
        self.save(data)

    @contextmanager
    def task(self, record: dict, *, model, max_tokens: int, get_state,
             before_start=None, on_compacting=None, on_compacted=None):
        """Own one execution's lock, recovery, compaction, record and cleanup."""
        with self.locked():
            data = self.load()
            if any(old['id'] == record['id'] for old in data['tasks']):
                raise ValueError('Task id already exists; inspect its result before submitting a new assignment')
            if before_start:
                before_start(data, record)
            for old in data['tasks']:
                if old['status'] == 'running' or old['status'] == 'awaiting_user' and not old.get('ended_at'):
                    old.update(status='interrupted', ended_at=now())
            history = self.recover_messages(data['messages'])
            recorded = {m['tool_call_id'] for m in data['messages'] if m['role'] == 'tool'}
            recovered = [m for m in history if m['role'] == 'tool' and m['tool_call_id'] not in recorded]
            data['messages'] = history
            record.update(role=self.role, status='running', started_at=now())
            data['tasks'].append(record)
            try:
                self.save(data)
                history = self.compact(data, history, model, max_tokens,
                                       on_compacting=on_compacting, on_compacted=on_compacted)
                yield SessionTask(self, data, record, history, recovered)
                if record['status'] == 'running':
                    record['status'] = 'completed'
            except BaseException as exc:
                record.update(status='interrupted' if isinstance(
                    exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else 'failed',
                    error=str(exc))
                raise
            finally:
                record['ended_at'] = now()
                state = get_state()
                if state is not None:
                    self.save_conversation(data, record, state)
                else:
                    self.save(data)

    def compact(self, data: dict, history: list[dict], model, max_tokens: int, *,
                on_compacting=None, on_compacted=None) -> list[dict]:
        messages, compacted = prepare_history(history, data.get('context_memory'), model,
                                              max_tokens, on_compacting=on_compacting)
        if messages is history:
            return history
        archive = self.folder / "archives" / self.role
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{time.time_ns()}.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        data["messages"] = messages
        data.pop("context_memory", None)
        self.save(data)
        if compacted and on_compacted:
            on_compacted()
        return messages

    @staticmethod
    def recover_messages(messages: list[dict]) -> list[dict]:
        """Close unrecorded tool results without replaying possibly executed writes."""
        recovered = []
        pending = {}
        for message in messages:
            if message["role"] == "system":
                continue
            if message["role"] == "assistant":
                pending.update({call["id"]: call["function"]["name"]
                                for call in message.get("tool_calls", [])})
            elif message["role"] == "tool":
                pending.pop(message["tool_call_id"], None)
            recovered.append(message)
        for identifier, name in pending.items():
            content = (
                'User question was interrupted before an answer was recorded. Do not infer an answer or approval. '
                'Check pending questions and current project state; continue only work already authorized by the current request.'
                if name == 'request_user_decision' else
                'Execution interrupted before a durable result was recorded. The action may have run. '
                'Inspect actual files, processes and receipts before retrying; do not assume success.'
            )
            recovered.append({"role": "tool", "tool_call_id": identifier, "name": name,
                              "content": content,
                              "is_error": True})
        return recovered
