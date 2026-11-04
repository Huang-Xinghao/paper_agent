"""Versioned proposal review contract shared by the API, worker and workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROPOSAL = "refine-logs/FINAL_PROPOSAL.md"
CANVAS = "refine-logs/IDEA_CANVAS.json"
RECORD = "refine-logs/IDEA_REVIEW.json"

class Position(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class ModuleData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=10000)
    input: str = Field(default="", max_length=10000)
    steps: str = Field(default="", max_length=20000)
    output: str = Field(default="", max_length=10000)
    parameters: str = Field(default="", max_length=10000)
    limits: str = Field(default="", max_length=10000)
    example: str = Field(default="", max_length=10000)
    sectionIds: list[str] = Field(default_factory=list, max_length=30)
    sourceContent: str = Field(default="", max_length=80000)
    disabled: bool = False
    needsReview: bool = False


class CanvasNode(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    position: Position
    data: ModuleData


class CanvasEdge(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)


class ReviewSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=3000)
    details: str = Field(max_length=80000)
    focus: bool = False
    edited: bool = False


class ProposalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[ReviewSection] = Field(max_length=30)
    feedback: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def unique_sections(self):
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("审阅内容标识不能重复")
        return self


class CanvasDraft(BaseModel):
    version: int = Field(default=1, ge=1, le=1)
    sourceHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    nodes: list[CanvasNode] = Field(max_length=100)
    edges: list[CanvasEdge] = Field(max_length=300)
    changes: list[str] = Field(default_factory=list, max_length=100)
    review: ProposalReview | None = None

    @model_validator(mode="after")
    def relationships(self):
        if self.review:
            section_ids = {section.id for section in self.review.sections}
            if any(s not in section_ids for node in self.nodes for s in node.data.sectionIds):
                raise ValueError("方法模块引用了不存在的方案部分")
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes) or len({e.id for e in self.edges}) != len(
            self.edges
        ):
            raise ValueError("节点和连线标识不能重复")
        pairs = set()
        for edge in self.edges:
            pair = (edge.source, edge.target)
            if (
                edge.source not in ids
                or edge.target not in ids
                or edge.source == edge.target
                or pair in pairs
            ):
                raise ValueError("连线必须连接两个不同的现有节点，且不能重复")
            pairs.add(pair)
        if any(len(change) > 1000 for change in self.changes):
            raise ValueError("修改记录过长")
        return self



class ReviewImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(direct|related)$")
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=4000)
    sectionIds: list[str] = Field(default_factory=list, max_length=30)


class ReviewQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=3000)
    sectionIds: list[str] = Field(default_factory=list, max_length=30)


class SourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sourceTitle: str = Field(min_length=1, max_length=300)
    sectionIds: list[str] = Field(min_length=1, max_length=30)


class RevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review: ProposalReview
    nodes: list[CanvasNode] = Field(max_length=100)
    edges: list[CanvasEdge] = Field(max_length=300)
    summary: str = Field(min_length=1, max_length=3000)
    impacts: list[ReviewImpact] = Field(max_length=60)
    questions: list[ReviewQuestion] = Field(max_length=30)
    coverage: list[SourceCoverage] = Field(max_length=60)

    @model_validator(mode="after")
    def references(self):
        ids = {s.id for s in self.review.sections}
        if not ids or any(not s.summary.strip() or not s.details.strip() for s in self.review.sections):
            raise ValueError("修订方案的各部分必须有完整摘要和正文")
        if any(not node.data.sectionIds for node in self.nodes):
            raise ValueError("方法图的每个模块必须关联方案正文")
        if any(ref not in ids for item in [*self.impacts, *self.questions, *self.coverage] for ref in item.sectionIds):
            raise ValueError("修订报告引用了不存在的方案部分")
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("待决策问题标识重复")
        return self


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def owned_path(root: Path, name: str) -> Path:
    root = root.resolve()
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("方案审阅文件不能位于项目外或使用符号链接")
    return path


def atomic_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".proposal-review-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def draft_hash(draft: CanvasDraft) -> str:
    return sha(json_bytes(draft.model_dump()))


def load_draft(root: Path, expected_sha: str | None = None) -> CanvasDraft:
    content = owned_path(root, CANVAS).read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("审阅草稿超过 2 MiB")
    if expected_sha is not None and sha(content) != expected_sha:
        raise ValueError("审阅草稿已变化，请重新加载后提交")
    draft = CanvasDraft.model_validate_json(content)
    if draft.sourceHash != sha(owned_path(root, PROPOSAL).read_bytes()):
        raise ValueError("原提案已变化，请先核对新版")
    return draft


def load_record(root: Path):
    path = owned_path(root, RECORD)
    if not path.exists():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("方案修订记录过大")
    return json.loads(path.read_bytes())


def pending_refine(state) -> bool:
    cp = state.get("expert_checkpoint") or {}
    return (state.get("mode") == "expert" and state.get("status") == "awaiting_user"
            and state.get("current_node") == "refine" and cp.get("node") == "refine"
            and cp.get("status") == "pending" and cp.get("next_node") == "experiment_plan")


def review_status(root: Path, draft: CanvasDraft | None):
    try:
        record = load_record(root)
        state_path = owned_path(root, "workflow-state.json")
        state = json.loads(state_path.read_bytes()) if state_path.exists() else {}
        synced = bool(record and draft and record.get("draftHash") == draft_hash(draft)
                      and draft.sourceHash == sha(owned_path(root, PROPOSAL).read_bytes()))
        revision = None if not record else {
            key: record.get(key) for key in ["id", "summary", "impacts", "questions", "diff", "createdAt", "approvedAt", "formalHash", "frozenPath"]
        }
        if revision:
            revision["synced"] = synced
            revision["approved"] = bool(record.get("approvedAt"))
        return {"revision": revision, "canApprove": bool(pending_refine(state) and synced
                and not record.get("questions") and not record.get("approvedAt")),
                "context": "checkpoint" if pending_refine(state) else "auto" if state.get("mode") == "auto" else "historical"}
    except (ValueError, OSError, KeyError):
        return {"revision": None, "canApprove": False, "context": "unavailable"}


def require_candidate(root: Path, expected_sha: str):
    draft = load_draft(root, expected_sha)
    record = load_record(root)
    if not record or record.get("draftHash") != draft_hash(draft):
        raise ValueError("当前修改尚未由 Agent 统一修订，请先提交修订并复审")
    if record.get("questions"):
        raise ValueError("仍有待决策问题，请补充意见并提交 Agent 修订")
    if record.get("approvedAt"):
        raise ValueError("这一版本已批准，请勿重复提交")
    return draft, record


def guard_legacy_approval(root: Path):
    """旧网页与 CLI 也不能绕过新增审阅草稿，自动模式不调用此门禁。"""
    path = owned_path(root, CANVAS)
    if not path.exists():
        return
    draft = CanvasDraft.model_validate_json(path.read_bytes())
    record = load_record(root)
    if record and record.get("approvedAt") and record.get("draftHash") == draft_hash(draft):
        if record.get("formalHash") == sha(owned_path(root, PROPOSAL).read_bytes()):
            return
    if record or draft.changes or draft.review and (draft.review.feedback.strip() or any(s.edited for s in draft.review.sections)):
        raise ValueError("存在方案审阅草稿，请在方案审阅中统一修订、复审并批准")


def source_titles(source: str):
    titles = []
    fence = None
    for line in source.splitlines():
        delimiter = re.match(r"^\s*(`{3,}|~{3,})", line)
        if delimiter:
            if fence is None:
                fence = delimiter[1][0]
            elif fence == delimiter[1][0]:
                fence = None
        match = not fence and re.match(r"^##\s+(.+?)\s*#*$", line)
        if match:
            titles.append(match[1])
    return titles


def render_proposal(draft: CanvasDraft) -> str:
    if not draft.review or not draft.review.sections:
        raise ValueError("没有统一的方案正文")
    # 正式 MD 只编译统一正文；摘要和画布是阅读投影，不另拼一套方法。
    return "# 最终研究方案\n\n" + "\n\n".join(
        f"## {section.title}\n\n{section.details.strip()}" for section in draft.review.sections) + "\n"


def write_group(root: Path, changes: dict[str, bytes]):
    targets = {name: owned_path(root, name) for name in changes}
    previous = {name: path.read_bytes() if path.exists() else None for name, path in targets.items()}
    applied = []
    try:
        for name, content in changes.items():
            atomic_bytes(targets[name], content)
            applied.append(name)
    except BaseException:
        for name in reversed(applied):
            if previous[name] is None:
                targets[name].unlink(missing_ok=True)
            else:
                atomic_bytes(targets[name], previous[name])
        raise


def publish_candidate(root: Path, expected_sha: str):
    """在工作流锁内调用；正式文件和冻结副本来自用户复审的同一正文。"""
    from datetime import datetime, timezone
    draft, record = require_candidate(root, expected_sha)
    before = owned_path(root, PROPOSAL).read_bytes()
    formal = render_proposal(draft).encode("utf-8")
    formal_hash = sha(formal)
    frozen = f"refine-logs/approved-proposals/{formal_hash}.md"
    existing = owned_path(root, frozen)
    if existing.exists() and existing.read_bytes() != formal:
        raise ValueError("冻结版本内容不一致")
    draft.sourceHash = formal_hash
    record = {**record, "approvedAt": datetime.now(timezone.utc).isoformat(),
              "formalHash": formal_hash, "frozenPath": frozen, "draftHash": draft_hash(draft)}
    changes = {f"refine-logs/review-history/{record['id']}-before-approval.md": before,
               frozen: formal, PROPOSAL: formal, CANVAS: json_bytes(draft.model_dump()), RECORD: json_bytes(record)}
    write_group(root, changes)
    return {"proposal_review_id": record["id"], "frozen_report": frozen}
