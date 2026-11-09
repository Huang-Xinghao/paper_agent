"""Project-owned role conversations and explicit phase assignments."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
import time
from pathlib import Path
from uuid import uuid4


ROLE_DESCRIPTIONS = {
    "main": "Coordinate the research goal, decisions, assignments and evidence-based acceptance. Respect user checkpoints and execution budgets.",
    "idea_generator": "Develop evidence-backed research questions, literature and data understanding, ideas, novelty assessments and the final proposal.",
    "experiment": "Plan, implement and execute approved experiments; verify real execution receipts and explain results without inventing evidence.",
    "paper_writer": "Plan the paper, create figures, write, compile and revise using verified project evidence; distinguish claims from untested hypotheses.",
}

PHASE_ROLES = {
    **dict.fromkeys(("literature", "data_search", "ideas", "novelty", "research_refine"), "idea_generator"),
    **dict.fromkeys(("experiment_plan", "experiment_bridge"), "experiment"),
    **dict.fromkeys(("paper_plan", "paper_figure", "paper_write", "paper_compile", "improvement"), "paper_writer"),
}


def revision_role(project: Path, node: str) -> str:
    """Retain the original author when main explicitly overrode a phase role."""
    path = project / 'workflow-state.json'
    state = json.loads(path.read_text()) if path.is_file() else {}
    role = state.get('node_results', {}).get(node, {}).get('role')
    return role if role in {'idea_generator', 'experiment', 'paper_writer'} else PHASE_ROLES[
        'research_refine' if node == 'refine' else node]


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


class RoleSession:
    def __init__(self, project: Path, role: str, *, channel: str = "execution"):
        if role not in ROLE_DESCRIPTIONS:
            raise ValueError(f"Unknown role: {role}")
        self.role = role
        self.folder = project / ".paper-agent" / "role-sessions"
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
        if value["role"] != self.role:
            raise ValueError("Role session identity mismatch")
        return value

    def isolate_legacy_discussion(self, data):
        """Archive old mixed memory and keep identifiable execution exchanges only."""
        if self.channel != 'execution' or self.role == 'main' or data.get('channel') == 'execution':
            return
        history, kept, in_assignment = data['messages'], [], True
        for message in history:
            if message.get('role') == 'user':
                content = message.get('content', '')
                if isinstance(content, str):
                    if content.startswith('Current assignment ['):
                        in_assignment = True
                    elif not content.startswith('Completion checks failed:'):
                        # Untagged chat and summaries of mixed memory cannot be
                        # promoted to scientific instructions during migration.
                        in_assignment = False
            if in_assignment:
                kept.append(message)
        if kept != history:
            archive = self.folder / 'archives' / self.role / 'legacy-mixed'
            archive.mkdir(parents=True, exist_ok=True)
            (archive / f'{time.time_ns()}.json').write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            data['messages'] = kept
            data.pop('context_memory', None)
        data['channel'] = 'execution'
        self.save(data)

    def save(self, value: dict):
        value["updated_at"] = now()
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def compact(self, data: dict, history: list[dict], model, max_chars: int) -> list[dict]:
        from .context import resume_memory, summarize_messages
        messages = resume_memory(history, data.get("context_memory"))
        reused = messages is not history
        if len(json.dumps(messages, ensure_ascii=False)) > max_chars:
            # Reused memory is summarized only with the uncompressed tail;
            # the covered raw prefix never goes back into another model call.
            messages = [{"role": "user", "content": summarize_messages(model, messages, max_chars)}]
        elif not reused:
            return history
        archive = self.folder / "archives" / self.role
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{time.time_ns()}.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        data["messages"] = messages
        data.pop("context_memory", None)
        self.save(data)
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
            recovered.append({"role": "tool", "tool_call_id": identifier, "name": name,
                              "content": "Execution interrupted before a durable result was recorded. The action may have run. Inspect actual files, processes and receipts before retrying; do not assume success.",
                              "is_error": True})
        return recovered
