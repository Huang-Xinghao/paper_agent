"""Detached supervisor: persists progress even while the API is restarting."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from .store import REPO, data_root, event, now, read_json, rows, run_update


def stop_tree(process):
    """Experiments can own process groups, so also walk descendants."""
    try:
        parent = psutil.Process(process.pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    # SIGINT lets the experiment queue persist interrupted receipts.
    for item in processes:
        try:
            item.send_signal(signal.SIGINT)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=8)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)


def execute(run_id):
    from paper_agent.agent.workflow_cli import run_workflow
    from .runtime import check_runtime

    run = rows("SELECT * FROM runs WHERE id=?", (run_id,))[0]
    project = rows("SELECT * FROM projects WHERE id=?", (run["project_id"],))[0]
    options = json.loads(run["options"])
    options.pop("message_prompt", None)
    action = options.pop("action")
    token = options.pop("checkpoint_token", None)
    answer = options.pop("answer", "")
    if action == "review":
        from .app import checkpoint_token

        state = read_json(Path(project["path"]) / "workflow-state.json", {})
        if state.get("status") != "awaiting_user" or token != checkpoint_token(
            project, state
        ):
            raise ValueError("审阅文件已变化，请刷新后重新确认")
        # Use the same semantic revision contract as the terminal entry point.
        if answer.strip():
            options["advance_after_revision"] = True
            options["revise"] = (
                "用户已回答当前节点的专家审阅问题。先读取现有报告，将回答整合到当前节点全部相关产物，"
                "消除与回答冲突的建议，保留有效证据及用户回答以供追溯。"
                "不得仅改报告冒充新实验结果，不得篡改执行回执；实验变更须经现有工具执行验证。"
                "修订完成后由工作流自动进入下游，无需再次提问确认。"
                "\n当前审阅问题：\n" + "\n".join(
                    f"{index + 1}. {question}" for index, question in enumerate(
                        (state.get("expert_checkpoint") or {}).get("questions", [])
                    )
                ) + "\n用户回答：\n" + answer
            )
        else:
            options["continue_run"] = True
    options["max_iterations"] = options.pop("max_tool_rounds", 100)
    on_event = lambda record: event(project["id"], record["kind"], record, run_id)
    if action == "revise":
        from paper_agent.agent.node_task import run_node_task
        state = run_node_task(Path(project["path"]), options["node_task"], options["prompt"],
                              data_root() / "runs" / run_id, model=options.get("model"),
                              timeout=options.get("timeout", 600),
                              max_iterations=options["max_iterations"], on_event=on_event)
    else:
        options.pop("node_task", None)
        check_runtime()
        state = run_workflow(Path(project["path"]), on_event=on_event, **options)
    output = data_root() / "runs" / run_id / "result.json"
    output.write_text(
        json.dumps({"status": state.status, "error": state.error}), encoding="utf-8"
    )


def supervise(run_id):
    import fcntl

    run = rows("SELECT * FROM runs WHERE id=?", (run_id,))[0]
    project = rows("SELECT * FROM projects WHERE id=?", (run["project_id"],))[0]
    root = Path(project["path"])
    folder = data_root() / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    process = None
    stopped = False

    def request_stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        with (root / ".web-run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_update(
                run_id,
                status="running",
                pid=os.getpid(),
                pid_created=psutil.Process().create_time(),
            )
            event(project["id"], "run_started", {}, run_id)
            with (folder / "output.log").open("wb") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "paper_agent.api.worker",
                        "--execute",
                        run_id,
                    ],
                    cwd=REPO,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                while process.poll() is None:
                    if stopped or (folder / "stop").exists():
                        stopped = True
                        run_update(run_id, status="stopping")
                        stop_tree(process)
                        break
                    time.sleep(0.7)
                process.wait()
            result = read_json(folder / "result.json", {})
            status = "interrupted" if stopped else result.get("status", "failed")
            error = result.get("error")
            if status == "failed" and not error:
                error = f"运行进程退出（退出码 {process.returncode}），请查看运行日志。"
            run_update(run_id, status=status, finished_at=now(), error=error)
            event(
                project["id"],
                "run_finished",
                {"status": status, "error": error},
                run_id,
            )
    except BaseException as exc:
        if process is not None and process.poll() is None:
            stop_tree(process)
        run_update(run_id, status="failed", finished_at=now(), error=str(exc))
        event(
            project["id"],
            "run_finished",
            {"status": "failed", "error": str(exc)},
            run_id,
        )


if __name__ == "__main__":
    if sys.argv[1] == "--execute":
        try:
            execute(sys.argv[2])
        except Exception as exc:
            import traceback
            traceback.print_exc()
            output = data_root() / "runs" / sys.argv[2] / "result.json"
            output.write_text(json.dumps({"status": "failed", "error": str(exc)},
                                         ensure_ascii=False), encoding="utf-8")
            sys.exit(1)
    else:
        supervise(sys.argv[1])
