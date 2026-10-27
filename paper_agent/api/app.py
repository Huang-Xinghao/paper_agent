"""Local-first HTTP API; run behind authentication before exposing remotely."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psutil
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .schemas import HealthView, ProjectView
from . import auth
from .library import router as library_router
from .intent import route_message
from .dispatch import RevisionTask, dispatch_message
from .store import (
    REPO,
    connection,
    data_root,
    event,
    initialize,
    now,
    read_json,
    rows,
    run_update,
)
from paper_agent.agent.workflow_cli import WORKFLOW_NODE_NAMES

LABELS = [
    "文献研究",
    "数据检索",
    "创意生成",
    "研究精炼",
    "实验规划",
    "实验执行",
    "论文规划",
    "论文绘图",
    "论文写作",
    "编译交付",
    "审阅改进",
]
ACTIVE = ("queued", "running", "stopping")
READ_SUFFIXES = {
    ".md",
    ".txt",
    ".log",
    ".json",
    ".csv",
    ".py",
    ".tex",
    ".bib",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
    ".pdf",
}
EDIT_SUFFIXES = {".md", ".txt", ".tex", ".bib", ".py"}
MAX_TEXT = 2 * 1024 * 1024


def allowed_roots():
    configured = os.environ.get("PAPER_AGENT_PROJECT_ROOTS")
    return (
        [Path(p).expanduser().resolve() for p in configured.split(os.pathsep)]
        if configured
        else [
            Path("/data/xinghao/paper-agent-projects").resolve(),
            (data_root() / "projects").resolve(),
        ]
    )


def valid_project_path(path):
    path = Path(path).expanduser().resolve()
    if not any(path != root and path.is_relative_to(root) for root in allowed_roots()):
        raise HTTPException(400, "项目必须位于已配置的项目根目录内")
    if not path.is_dir():
        raise HTTPException(400, "项目目录不存在")
    return path


def project_record(project_id):
    found = rows("SELECT * FROM projects WHERE id=?", (project_id,))
    if not found:
        raise HTTPException(404, "项目不存在")
    project = found[0]
    valid_project_path(project["path"])
    return project


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_path(project, relative):
    parts = Path(relative).parts
    if (
        not relative
        or Path(relative).is_absolute()
        or any(p.startswith(".") for p in parts)
    ):
        raise HTTPException(400, "文件路径无效")
    root = Path(project["path"]).resolve()
    path = (root / relative).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.suffix.lower() not in READ_SUFFIXES
    ):
        raise HTTPException(404, "文件不存在或不可访问")
    return path


def reconcile():
    for run in rows(
        "SELECT * FROM runs WHERE status IN ('queued','running','stopping')"
    ):
        if run["pid"]:
            try:
                process = psutil.Process(run["pid"])
                if (
                    abs(process.create_time() - (run["pid_created"] or 0)) < 0.1
                    and process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                ):
                    continue
            except psutil.NoSuchProcess:
                pass
        elif (
            datetime.now(timezone.utc) - datetime.fromisoformat(run["created_at"])
        ).total_seconds() < 30:
            continue
        run_update(
            run["id"],
            status="interrupted",
            finished_at=now(),
            error="运行管理进程已退出，可从已有节点恢复。",
        )
        event(run["project_id"], "run_finished", {"status": "interrupted"}, run["id"])


def register(name, path):
    path = valid_project_path(path)
    identifier = uuid.uuid4().hex[:16]
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO projects VALUES(?,?,?,?)",
            (identifier, name, str(path), now()),
        )
    return rows("SELECT * FROM projects WHERE path=?", (str(path),))[0]


@asynccontextmanager
async def lifespan(_):
    if os.environ.get("PAPER_AGENT_PUBLIC_HOSTS") and not auth.enabled():
        raise RuntimeError("Public access requires PAPER_AGENT_PASSWORD_HASH")
    initialize()
    auth.initialize()
    for root in allowed_roots():
        if root.is_dir():
            for candidate in sorted(root.iterdir()):
                if (
                    candidate.is_dir()
                    and not candidate.name.startswith(".")
                    and (candidate / "workflow-state.json").is_file()
                ):
                    try:
                        register(candidate.name, candidate)
                    except HTTPException:
                        pass
    reconcile()
    yield


app = FastAPI(
    title="Paper Agent Workbench API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    + [host.strip() for host in os.environ.get("PAPER_AGENT_PUBLIC_HOSTS", "").split(",") if host.strip()],
)
app.include_router(auth.router)
app.include_router(library_router)


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit

        local = urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"}
        allowed = origin in os.environ.get("PAPER_AGENT_PUBLIC_ORIGINS", "").split(",")
        if not local and not allowed:
            return Response("Origin not allowed", status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and auth.enabled() and not origin:
        return Response("Origin required", status_code=403)
    if request.url.path.startswith("/api") and request.url.path not in {"/api/auth/session", "/api/auth/login"}:
        if not auth.authenticated(request):
            return Response('{"detail":"请先登录"}', status_code=401, media_type="application/json")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    path: str | None = None


class RunCreate(BaseModel):
    action: Literal["start", "resume", "review", "revise"] = "start"
    prompt: str = Field(default="", max_length=20000)
    mode: Literal["auto", "expert"] | None = None
    start_node: str | None = None
    stop_after: str | None = None
    max_tool_rounds: int = Field(default=100, ge=1, le=500)
    timeout: float = Field(default=600, gt=0, le=7200, allow_inf_nan=False)
    paper_review_timeout: float = Field(default=1800, gt=0, le=14400, allow_inf_nan=False)
    paper_review_round2_timeout: float = Field(default=1200, gt=0, le=14400, allow_inf_nan=False)
    model: str | None = Field(default=None, max_length=200)
    answer: str = Field(default="", max_length=20000)
    checkpoint_token: str | None = None
    node_task: RevisionTask | None = None

    @model_validator(mode="after")
    def nodes(self):
        if (self.action == "revise") != (self.node_task is not None):
            raise ValueError("局部修改需要且仅允许携带 node_task")
        for node in [self.start_node, self.stop_after]:
            if node is not None and node not in WORKFLOW_NODE_NAMES:
                raise ValueError("未知工作流节点")
        return self


class FileEdit(BaseModel):
    content: str = Field(max_length=MAX_TEXT)
    sha256: str


def checkpoint_token(project, state):
    checkpoint = state.get("expert_checkpoint") or {}
    hashes = {}
    for relative in checkpoint.get("files", []):
        try:
            hashes[relative] = digest(file_path(project, relative))
        except HTTPException:
            hashes[relative] = None
    return hashlib.sha256(
        json.dumps(
            {"checkpoint": checkpoint, "hashes": hashes}, sort_keys=True
        ).encode()
    ).hexdigest()


def terminal_projects():
    """Recognize CLI-owned runs without taking ownership of their processes."""
    paths = set()
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = process.info["cmdline"] or []
            if not any(
                "paper_agent" in arg
                or Path(arg).name in {"paper-workflow", "paper-agent", "run-workflow"}
                for arg in args
            ):
                continue
            for index, argument in enumerate(args):
                value = (
                    args[index + 1]
                    if argument == "--project" and index + 1 < len(args)
                    else (
                        argument.split("=", 1)[1]
                        if argument.startswith("--project=")
                        else None
                    )
                )
                if value:
                    path = Path(value)
                    if not path.is_absolute():
                        path = Path(process.cwd()) / path
                    paths.add(str(path.resolve()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return paths


def detail(project, terminal_paths=None):
    state = read_json(Path(project["path"]) / "workflow-state.json", {})
    runs = rows(
        "SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 30",
        (project["id"],),
    )
    for run in runs:
        run["options"] = json.loads(run["options"])
    # Management status overrides stale "running" files after an interrupted run.
    latest = runs[0] if runs else None
    terminal_paths = terminal_projects() if terminal_paths is None else terminal_paths
    status = latest["status"] if latest else state.get("status", "idle")
    if project["path"] in terminal_paths:
        status = "external_running"
    elif not latest and status == "running":
        status = "interrupted"
    progress = read_json(Path(project["path"]) / "workflow-progress.json", {})
    review_progress = progress.get("improvement", {})
    if (status in ACTIVE and review_progress.get("stage") == "review"
            and review_progress.get("stage_started_at")):
        try:
            progress["improvement"] = {**review_progress, "elapsed_seconds": max(0, round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(review_progress["stage_started_at"])).total_seconds(), 1))}
        except (ValueError, TypeError):
            pass
    return {
        **project,
        "status": status,
        "state": state,
        "progress": progress,
        "runs": runs,
        "checkpoint_token": checkpoint_token(project, state),
    }


@app.get("/api/health", response_model=HealthView)
def health():
    return {
        "status": "ok",
        "runtime": sys.executable,
        "project_roots": [str(p) for p in allowed_roots()],
        "nodes": [
            {"id": key, "label": label}
            for key, label in zip(WORKFLOW_NODE_NAMES, LABELS)
        ],
        "concurrency": 1,
    }


@app.get("/api/projects", response_model=list[ProjectView])
def projects():
    reconcile()
    result = []
    terminal_paths = terminal_projects()
    for project in rows(
        "SELECT * FROM projects WHERE id NOT IN "
        "(SELECT project_id FROM archived_projects) ORDER BY created_at DESC"
    ):
        try:
            valid_project_path(project["path"])
            result.append(detail(project, terminal_paths))
        except HTTPException:
            continue
    return result


@app.post("/api/projects", status_code=201, response_model=ProjectView)
def create_project(body: ProjectCreate):
    if body.path:
        project = register(body.name.strip() or "未命名项目", body.path)
    else:
        root = allowed_roots()[0]
        root.mkdir(parents=True, exist_ok=True)
        folder = root / ("project-" + uuid.uuid4().hex[:10])
        folder.mkdir()
        project = register(body.name.strip() or "未命名项目", folder)
    event(project["id"], "project_registered", {"name": project["name"]})
    return detail(project)


@app.get("/api/projects/{project_id}", response_model=ProjectView)
def get_project(project_id: str):
    reconcile()
    return detail(project_record(project_id))


@app.post("/api/projects/{project_id}/runs", status_code=202)
def start_run(project_id: str, body: RunCreate):
    return create_run(project_id, body)


class MessageCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    mode: Literal["auto", "expert"] = "expert"
    model: str | None = Field(default=None, max_length=200)


@app.post("/api/projects/{project_id}/messages")
def send_message(project_id: str, body: MessageCreate):
    if not body.prompt.strip():
        raise HTTPException(400, "请输入消息")
    reconcile()
    project = project_record(project_id)
    state = read_json(Path(project["path"]) / "workflow-state.json", {})
    latest = rows("SELECT status FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,))
    project["status"] = latest[0]["status"] if latest else state.get("status", "idle")
    history = rows("SELECT data FROM events WHERE project_id=? AND kind='conversation_turn' ORDER BY id DESC LIMIT 8",
                   (project_id,))
    try:
        decision = dispatch_message(project, body.prompt,
                                    [json.loads(row["data"]) for row in reversed(history)], body.model)
    except Exception:
        raise HTTPException(503, "暂时无法判断消息意图，未启动任务。请稍后重试。") from None
    tool = decision["tool"]
    if tool == "reply":
        reply = decision["reply"]
        event(project_id, "conversation_turn", {"prompt": body.prompt, "reply": reply, "intent": "chat"})
        return {"intent": "chat", "reply": reply}
    arguments = decision["arguments"]
    if tool == "run_workflow":
        if state:
            raise HTTPException(409, "此项目已有研究成果；新研究请新建项目，修改现有成果请说明目标")
        request = RunCreate(prompt=body.prompt + "\n任务上下文：\n" + arguments["instruction"],
                            mode=body.mode, model=body.model)
    elif tool == "resume_workflow":
        if state.get("status") not in {"failed", "running", "interrupted", "awaiting_results"}:
            raise HTTPException(409, "没有可恢复的失败或中断任务；请说明需要修改的具体产物")
        request = RunCreate(action="resume", mode=body.mode, model=body.model)
    elif tool == "revise_artifact":
        request = RunCreate(action="revise", prompt=body.prompt, node_task=arguments, model=body.model)
    else:
        raise HTTPException(503, "无效的调度结果，未启动任务")
    result = create_run(project_id, request, route_initial=False, message_prompt=body.prompt)
    event(project_id, "conversation_turn", {"prompt": body.prompt, "reply": "任务已提交。",
          "intent": tool, "run_id": result["id"]})
    event(project_id, "message_dispatched", {"tool": tool, **arguments}, result["id"])
    return JSONResponse(result, status_code=202)


def create_run(project_id: str, body: RunCreate, *, route_initial=True, message_prompt=None):
    project = project_record(project_id)
    initial_state = read_json(Path(project["path"]) / "workflow-state.json", {})
    if route_initial and body.action == "start" and not initial_state:
        if not body.prompt.strip():
            raise HTTPException(400, "请输入研究目标或问题")
        history = rows(
            "SELECT data FROM events WHERE project_id=? AND kind='conversation_turn' ORDER BY id DESC LIMIT 8",
            (project_id,),
        )
        try:
            decision = route_message(body.prompt, [json.loads(row["data"]) for row in reversed(history)], body.model)
        except Exception:
            raise HTTPException(503, "暂时无法判断请求意图，未启动研究。请稍后重试。") from None
        if decision["intent"] != "research":
            turn = {"prompt": body.prompt, "reply": decision["reply"], "intent": decision["intent"]}
            event(project_id, "conversation_turn", turn)
            return JSONResponse({"intent": decision["intent"], "reply": decision["reply"]}, status_code=200)
        body = body.model_copy(update={"prompt": decision["research_prompt"]})
    reconcile()
    terminal_paths = terminal_projects()
    registered_paths = {item["path"] for item in rows("SELECT path FROM projects")}
    if terminal_paths & registered_paths:
        raise HTTPException(
            409, "已有终端工作流正在运行，请等待完成或从原终端停止后再启动"
        )
    identifier = uuid.uuid4().hex
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # First release serializes web runs globally to avoid cross-project GPU contention.
        if db.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running','stopping')"
        ).fetchone():
            raise HTTPException(409, "已有项目正在运行，请等待完成或先停止当前运行")
        state = read_json(Path(project["path"]) / "workflow-state.json", {})
        options = body.model_dump(exclude_none=True)
        if message_prompt is not None:
            options["message_prompt"] = message_prompt
        if body.action == "revise":
            if not state:
                raise HTTPException(409, "项目尚无产物，请先开始研究")
            if not body.prompt.strip():
                raise HTTPException(400, "请输入具体修改要求")
            root = Path(project["path"]).resolve()
            for name in body.node_task.files:
                if not (root / name).resolve().is_relative_to(root):
                    raise HTTPException(400, "目标文件位于项目外")
            if (body.node_task.node != "improvement" and
                    not any((root / name).is_file() for name in body.node_task.files)):
                raise HTTPException(400, "未找到要修改的现有产物或生成脚本")
        elif body.action == "review":
            if state.get("status") != "awaiting_user":
                raise HTTPException(409, "项目没有待审阅检查点")
            if body.checkpoint_token != checkpoint_token(project, state):
                raise HTTPException(409, "报告或检查点已变化，请刷新后重新确认")
            for key in ["mode", "prompt", "start_node", "stop_after"]:
                options.pop(key, None)
            latest = db.execute(
                "SELECT options FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if latest:
                previous = json.loads(latest["options"])
                # Use this request’s round budget, including the current default.
                # Old runs must not silently restore an obsolete default on review.
                for key in ["timeout", "model"]:
                    if key in previous:
                        options[key] = previous[key]
        elif body.action == "resume":
            if not state or state.get("status") == "awaiting_user":
                raise HTTPException(409, "请先完成专家审阅，或创建研究任务")
            options["start_node"] = (
                body.start_node or state.get("failed_node") or state.get("current_node")
            )
            options["prompt"] = body.prompt.strip() or state.get("task_prompt", "")
            # The CLI supports changing collaboration mode at a resume node.
        elif not body.prompt.strip():
            raise HTTPException(400, "请输入研究目标")
        elif state and not body.start_node:
            raise HTTPException(409, "已有项目请使用恢复运行，或明确选择起始节点")
        db.execute(
            "INSERT INTO runs(id,project_id,status,options,created_at) VALUES(?,?,?,?,?)",
            (
                identifier,
                project_id,
                "queued",
                json.dumps(options, ensure_ascii=False),
                now(),
            ),
        )
    folder = data_root() / "runs" / identifier
    folder.mkdir(parents=True)
    try:
        runtime = os.environ.get("PAPER_AGENT_RUNTIME_PYTHON", sys.executable)
        environment = os.environ.copy()
        environment["PATH"] = (
            str(Path(runtime).parent) + os.pathsep + environment.get("PATH", "")
        )
        with (folder / "supervisor.log").open("wb") as log:
            process = subprocess.Popen(
                [runtime, "-u", "-m", "paper_agent.api.worker", identifier],
                cwd=REPO,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # The detached worker also sets its identity. Reap only our short-lived handle.
        try:
            run_update(
                identifier,
                pid=process.pid,
                pid_created=psutil.Process(process.pid).create_time(),
            )
        except psutil.NoSuchProcess:
            pass
        import threading

        threading.Thread(target=process.wait, daemon=True).start()
    except Exception as exc:
        run_update(identifier, status="failed", error=str(exc), finished_at=now())
        raise HTTPException(500, "运行进程启动失败") from exc
    event(project_id, "run_requested", {"action": body.action}, identifier)
    return {"id": identifier, "status": "queued"}


@app.post("/api/projects/{project_id}/stop", status_code=202)
def stop_run(project_id: str):
    project_record(project_id)
    active = rows(
        "SELECT * FROM runs WHERE project_id=? AND status IN ('queued','running','stopping')",
        (project_id,),
    )
    if not active:
        raise HTTPException(409, "没有运行中的任务")
    run = active[0]
    (data_root() / "runs" / run["id"] / "stop").touch()
    run_update(run["id"], status="stopping")
    event(project_id, "stop_requested", {}, run["id"])
    return {"status": "stopping"}


@app.get("/api/projects/{project_id}/files")
def list_files(project_id: str):
    project = project_record(project_id)
    root = Path(project["path"])
    result = []
    for directory, folders, files in os.walk(root, followlinks=False):
        # Load research artifacts before large directories of run history.
        # Otherwise the file limit can hide results/ and paper/ behind logs.
        folders[:] = sorted(
            (f for f in folders if not f.startswith(".")
             and f not in {"__pycache__", "node_modules", "venv"}),
            key=lambda name: (name in {"experiment-runs", "logs", "runs"}, name),
        )
        for name in sorted(files):
            path = Path(directory) / name
            if name.startswith(".") or path.suffix.lower() not in READ_SUFFIXES:
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            stat = path.stat()
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
            if len(result) >= 5000:
                return {"files": result, "truncated": True}
    return {"files": result, "truncated": False}


@app.get("/api/projects/{project_id}/file")
def read_file(project_id: str, path: str):
    target = file_path(project_record(project_id), path)
    if target.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg"}:
        return {"kind": "binary", "path": path, "size": target.stat().st_size}
    if target.stat().st_size > MAX_TEXT:
        raise HTTPException(413, "文件超过 2 MiB，请下载查看")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HTTPException(415, "仅支持 UTF-8 文本") from exc
    return {
        "kind": "text",
        "path": path,
        "content": content,
        "sha256": digest(target),
        "editable": target.suffix.lower() in EDIT_SUFFIXES
        and not path.startswith("experiment-runs/"),
    }


@app.get("/api/projects/{project_id}/download")
def download(project_id: str, path: str):
    target = file_path(project_record(project_id), path)
    # SVG is downloadable only; do not execute project-supplied scripts in this origin.
    disposition = (
        "inline"
        if target.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
        else "attachment"
    )
    return FileResponse(
        target, filename=target.name, content_disposition_type=disposition
    )


@app.put("/api/projects/{project_id}/file")
def edit_file(project_id: str, path: str, body: FileEdit):
    import fcntl

    project = project_record(project_id)
    target = file_path(project, path)
    if target.suffix.lower() not in EDIT_SUFFIXES or path.startswith(
        "experiment-runs/"
    ):
        raise HTTPException(403, "此类文件不允许从界面修改")
    with (Path(project["path"]) / ".web-run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "运行期间不能编辑产物")
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running','stopping')",
                (project_id,),
            ).fetchone():
                raise HTTPException(409, "运行期间不能编辑产物")
            if digest(target) != body.sha256:
                raise HTTPException(409, "文件已被修改，请重新加载后编辑")
            import tempfile

            fd, temporary = tempfile.mkstemp(prefix=".web-edit-", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(body.content)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    event(project_id, "file_saved", {"path": path, "sha256": digest(target)})
    return read_file(project_id, path)


@app.get("/api/projects/{project_id}/log")
def log_tail(project_id: str):
    project_record(project_id)
    latest = rows(
        "SELECT id FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not latest:
        return {"content": ""}
    target = data_root() / "runs" / latest[0]["id"] / "output.log"
    if not target.is_file():
        return {"content": ""}
    with target.open("rb") as stream:
        stream.seek(max(0, target.stat().st_size - 100000))
        content = stream.read().decode("utf-8", errors="replace")
    return {"content": content}


@app.get("/api/projects/{project_id}/activity")
def activity(
    project_id: str,
    before: int | None = Query(default=None, ge=1),
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Page durable public execution events for chat history and polling recovery."""
    project_record(project_id)
    if before is not None and after is not None:
        raise HTTPException(400, "不能同时指定 before 和 after")
    clause, values = "", [project_id]
    if before is not None:
        clause = " AND id<?"
        values.append(before)
    elif after is not None:
        clause = " AND id>?"
        values.append(after)
    direction = "ASC" if after is not None else "DESC"
    records = rows(
        f"SELECT * FROM events WHERE project_id=?{clause} ORDER BY id {direction} LIMIT ?",
        (*values, limit + 1),
    )
    has_more = len(records) > limit
    records = records[:limit]
    if after is None:
        records.reverse()
    for record in records:
        record["data"] = json.loads(record["data"])
    return {"events": records, "has_more": has_more}


@app.get("/api/projects/{project_id}/events")
async def events(
    project_id: str, request: Request, after: int = Query(default=0, ge=0)
):
    project_record(project_id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "无效事件游标")

    async def stream():
        nonlocal cursor
        heartbeat = 0
        while not await request.is_disconnected():
            if heartbeat % 15 == 0 and not await asyncio.to_thread(auth.authenticated, request):
                break
            records = await asyncio.to_thread(
                rows,
                "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT 100",
                (project_id, cursor),
            )
            for record in records:
                cursor = record["id"]
                record["data"] = json.loads(record["data"])
                yield f"id: {cursor}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
            heartbeat += 1
            if heartbeat % 15 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


def main():
    import uvicorn

    uvicorn.run(
        "paper_agent.api.app:app",
        host="127.0.0.1",
        port=int(os.environ.get("PAPER_AGENT_API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
