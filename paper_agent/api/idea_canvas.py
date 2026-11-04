"""Human-owned proposal drafts. Saving a canvas never advances the workflow."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from paper_agent.proposal_review import CanvasDraft, review_status

from .store import connection, event, rows

router = APIRouter(prefix="/api/projects")
PROPOSAL = "refine-logs/FINAL_PROPOSAL.md"
CANVAS = "IDEA_CANVAS.json"


class CanvasSave(BaseModel):
    sha256: str = Field(pattern=r"^(|[a-f0-9]{64})$")
    draft: CanvasDraft


def paths(project_id):
    from .app import file_path, project_record

    project = project_record(project_id)
    proposal = file_path(project, PROPOSAL)
    target = proposal.parent / CANVAS
    # 固定目录仍需校验软链接，防止草案写入项目边界外。
    if target.is_symlink() or not target.resolve().is_relative_to(
        Path(project["path"]).resolve()
    ):
        raise HTTPException(400, "画布草案路径无效")
    return project, proposal, target


@router.get("/{project_id}/idea-canvas")
def read_canvas(project_id: str):
    from .app import digest

    project, proposal, target = paths(project_id)
    recent = rows("SELECT id,status,options,error,created_at,finished_at FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 10", (project_id,))
    operation = next(({"id": r["id"], "status": r["status"], "error": r["error"],
                       "createdAt": r["created_at"], "finishedAt": r["finished_at"],
                       "action": json.loads(r["options"]).get("action")}
                      for r in recent if json.loads(r["options"]).get("action") in {"proposal_revise", "proposal_approve"}), None)
    if operation:
        # 仅返回当前任务的阶段，不让上轮修订进度混入新任务。
        reports = rows("SELECT data,created_at FROM events WHERE project_id=? AND run_id=? AND kind='proposal_revision_progress' ORDER BY id DESC LIMIT 1",
                       (project_id, operation["id"]))
        if reports:
            data = json.loads(reports[0]["data"])
            operation["progress"] = {"stage": data.get("stage"),
                                     "receivedChars": data.get("receivedChars", 0),
                                     "updatedAt": reports[0]["created_at"]}
    if not target.exists():
        return {"sha256": "", "sourceHash": digest(proposal), "draft": None, **review_status(Path(project["path"]), None), "operation": operation}
    try:
        if target.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("oversize")
        content = target.read_bytes()
        draft = CanvasDraft.model_validate_json(content)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, "画布草案无法读取，请保留文件并检查格式") from exc
    return {
        "operation": operation,
        "sha256": hashlib.sha256(content).hexdigest(),
        "sourceHash": digest(proposal),
        "draft": draft,
        **review_status(Path(project["path"]), draft),
    }


@router.put("/{project_id}/idea-canvas")
def save_canvas(project_id: str, body: CanvasSave):
    import fcntl
    from .app import digest

    project, proposal, target = paths(project_id)
    serialized = json.dumps(body.draft.model_dump(), ensure_ascii=False, indent=2)
    if len(serialized.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(413, "画布草案超过 2 MiB")
    with (Path(project["path"]) / ".web-run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "运行期间不能保存画布草案")
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running','stopping')",
                (project_id,),
            ).fetchone():
                raise HTTPException(409, "运行期间不能保存画布草案")
            if digest(proposal) != body.draft.sourceHash:
                raise HTTPException(
                    409, "原提案已更新，请先核对新版；当前画布草稿仍保留"
                )
            if (digest(target) if target.exists() else "") != body.sha256:
                raise HTTPException(
                    409, "画布已被其他人修改，请先下载当前草稿再重新加载"
                )
            fd, temporary = tempfile.mkstemp(prefix=".canvas-", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(serialized)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            saved_hash = digest(target)
    event(
        project_id,
        "idea_canvas_saved",
        {"path": f"refine-logs/{CANVAS}", "sha256": saved_hash},
    )
    return {
        "sha256": saved_hash,
        "sourceHash": body.draft.sourceHash,
        "draft": body.draft,
        **review_status(Path(project["path"]), body.draft),
    }
