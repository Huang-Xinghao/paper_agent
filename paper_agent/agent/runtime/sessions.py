"""Project-owned role conversations and explicit phase assignments."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from paper_agent.agent.stages import BY_NODE, PHASE_ROLES

ROLE_DESCRIPTIONS = {
    "main": "Coordinate the research goal, decisions, assignments and evidence-based acceptance. Respect user checkpoints and execution budgets.",
    "idea_generator": "Develop evidence-backed research questions, literature and data understanding, ideas, novelty assessments and the final proposal.",
    "experiment": "Plan, implement and execute approved experiments; verify real execution receipts and explain results without inventing evidence.",
    "paper_writer": "Plan the paper, create figures, write, compile and revise using verified project evidence; distinguish claims from untested hypotheses.",
    "wiki_agent": "Read papers and project evidence, author grounded Wiki drafts, obtain independent review and publish verified Wiki records.",
}


def revision_role(project: Path, node: str) -> str:
    """Retain the original author when main explicitly overrode a phase role."""
    path = project / 'workflow-state.json'
    state = json.loads(path.read_text()) if path.is_file() else {}
    role = state.get('node_results', {}).get(node, {}).get('role')
    return role if role in {'idea_generator', 'experiment', 'paper_writer'} else BY_NODE[node].role


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
        from paper_agent.agent.image_store import ImageStore
        self.images = ImageStore(project)
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
        if value["role"] != self.role or value.get("channel") != self.channel:
            raise ValueError("Role session identity mismatch")
        # Preserve valid summary coverage when migrating an older inline transcript.
        memory = value.get('context_memory') or {}
        cutoff = memory.get('cutoff')
        covered = (isinstance(cutoff, int) and 0 < cutoff <= len(value['messages']) and
                   hashlib.sha256(json.dumps(value['messages'][:cutoff], ensure_ascii=False).encode()).hexdigest()
                   == memory.get('prefix_sha256'))
        self.images.externalize(value.get('messages', []))
        if covered:
            memory['prefix_sha256'] = hashlib.sha256(
                json.dumps(value['messages'][:cutoff], ensure_ascii=False).encode()).hexdigest()
        return value


    def save(self, value: dict):
        self.images.externalize(value.get('messages', []))
        value["updated_at"] = now()
        from paper_agent.storage import write_json
        write_json(self.path, value)

    def compact(self, data: dict, history: list[dict], model, max_tokens: int, *,
                on_compacting=None, on_compacted=None) -> list[dict]:
        from paper_agent.agent.context import compact_threshold, project_messages, request_tokens, resume_memory, summarize_messages
        messages = resume_memory(history, data.get("context_memory"))
        reused = messages is not history
        compacted = False
        if request_tokens(project_messages(messages, for_summary=True)) > compact_threshold(max_tokens):
            if on_compacting:
                on_compacting()
            # Reused memory is summarized only with the uncompressed tail;
            # the covered raw prefix never goes back into another model call.
            messages = [{"role": "user", "content": summarize_messages(model, messages, compact_threshold(max_tokens))}]
            compacted = True
        elif not reused:
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
