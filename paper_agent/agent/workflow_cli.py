"""Terminal entry point with live workflow progress and durable status files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .loop import PhaseAgent
from .workflow import IdeaNodeConfig, WorkflowConfig, WorkflowRunner, WorkflowState

WORKFLOW_NODE_NAMES = (
    "literature", "data_search", "ideas", "refine",
    "experiment_plan", "experiment_bridge",
    "paper_plan", "paper_figure", "paper_write",
    "paper_compile", "improvement",
)

def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_workflow(project: Path, prompt: str = "", *, max_iterations: int = 100,
                 timeout: float = 300, start_node: str | None = None,
                 config: WorkflowConfig | None = None,
                 idea_config: IdeaNodeConfig | None = None,
                 stop_after: str | None = None,
                 mode: str | None = None, continue_run: bool = False,
                 revise: str | None = None,
                 advance_after_revision: bool = False,
                 model: str | None = None,
                 paper_review_timeout: float | None = None,
                 paper_review_round2_timeout: float | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> WorkflowState:
    project = project.resolve()
    if not prompt.strip() and not start_node and not continue_run and revise is None:
        state_path = project / "workflow-state.json"
        if state_path.is_file():
            pending = WorkflowState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            if pending.status == "awaiting_user":
                return pending
        raise ValueError("首次运行需要任务 prompt；项目当前没有待审阅检查点")
    resuming = continue_run or revise is not None
    if resuming and (start_node or mode is not None or prompt.strip()):
        raise ValueError("--continue/--revise 不能与入口节点、mode 或新任务 prompt 组合")
    if continue_run and revise is not None:
        raise ValueError("--continue 与 --revise 不能同时使用")
    if revise is not None and not revise.strip():
        raise ValueError("审阅修改意见不能为空")
    saved = None
    if start_node or resuming:
        state_path = project / "workflow-state.json"
        if state_path.is_file():
            saved = WorkflowState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        if resuming:
            if saved is None or saved.status != "awaiting_user":
                raise ValueError("项目没有等待用户确认的专家检查点")
            prompt = ("请根据以下审阅意见修订当前节点产物：\n" + revise
                      if revise is not None else saved.task_prompt)
            if stop_after is None and saved.expert_checkpoint:
                stop_after = saved.expert_checkpoint.get("stop_after")
    if paper_review_timeout is not None:
        config = replace(config or WorkflowConfig(), paper_review_timeout=paper_review_timeout)
    if paper_review_round2_timeout is not None:
        config = replace(config or WorkflowConfig(), paper_review_round2_timeout=paper_review_round2_timeout)
    if (start_node == "improvement" and saved is not None
            and saved.status in {"failed", "running", "interrupted"}
            and (saved.improvement_checkpoint or
                 ((project / "paper/main_round0_original.pdf").is_file()
                  and (project / "paper/PAPER_IMPROVEMENT_LOG.md").is_file()))):
        config = replace(config or WorkflowConfig(), improvement_resume=True)
    # Preserve the structured stage checkpoint across web and terminal resumes.
    if (start_node == "improvement" and saved is not None
            and saved.status == "failed" and saved.failed_node == "improvement"
            and not saved.improvement_checkpoint
            and "Paper review round 2 did not complete:" in (saved.error or "")):
        # Compatibility with failures saved before structured checkpoints existed.
        config = replace(config or WorkflowConfig(), improvement_start_round=2, improvement_resume=True)
    if mode is not None:
        config = replace(config or WorkflowConfig(), mode=mode)
        if saved is not None:
            saved.mode = mode
    entry_node = start_node or stop_after or (config.start_node if config else "literature")
    started = time.monotonic()
    progress_path = project / "workflow-progress.json"
    progress: dict[str, Any] = {"status": "running", "current_node": entry_node,
                                "recent_events": []}

    def event(kind: str, **details: Any) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {"time": now, "kind": kind, **details}
        progress.update({"updated_at": now, "elapsed_seconds": round(time.monotonic() - started, 1),
                         "last_event": record})
        progress["recent_events"] = (progress["recent_events"] + [record])[-20:]
        if "node" in details:
            progress["current_node"] = details["node"]
        if "phase" in details:
            progress["phase"] = details["phase"]
        if "iteration" in details:
            progress["iteration"] = details["iteration"]
        if "tool" in details:
            progress["tool"] = details["tool"]
        _write_json(progress_path, progress)
        if on_event is not None:
            on_event(record)
        label = details.get("node") or details.get("phase") or progress["current_node"]
        suffix = f" #{details['iteration']}" if "iteration" in details else ""
        if "tool" in details:
            suffix += f" {details['tool']}"
        if "error" in details:
            suffix += f" error={details['error']}"
        print(f"[{now}] {label}: {kind}{suffix}", flush=True)

    def on_node_start(node: str, state: WorkflowState) -> None:
        progress["run_id"] = state.run_id
        progress["paper_status"] = state.paper_status
        progress.update(successful_node_executions=state.transitions,
                        fault_retries=state.fault_retry_count)
        _write_json(project / "workflow-state.json", state.to_dict())
        progress.pop("tool", None)
        progress.pop("iteration", None)
        event("node_start", node=node)

    def on_node_end(node: str, state: WorkflowState, result: Any) -> None:
        progress["paper_status"] = state.paper_status
        progress.update(successful_node_executions=state.transitions,
                        fault_retries=state.fault_retry_count)
        _write_json(project / "workflow-state.json", state.to_dict())
        event("node_end", node=node, artifact=result.artifact[:200])

    def on_node_progress(node: str, state: WorkflowState, details: dict[str, Any]) -> None:
        progress["paper_status"] = state.paper_status
        _write_json(project / "workflow-state.json", state.to_dict())
        progress["improvement"] = details if node == "improvement" else progress.get("improvement", {})
        event("node_progress", node=node, **details)

    agent = PhaseAgent(project, model=model, max_iterations=max_iterations, timeout=timeout,
                       on_progress=lambda kind, data: event(kind, **data))
    runner_options = {"config": config} if config is not None else {}
    if idea_config is not None:
        runner_options["idea_config"] = idea_config
    runner = WorkflowRunner(agent, on_node_start=on_node_start, on_node_end=on_node_end,
                            on_node_progress=on_node_progress,
                            **runner_options)
    event("workflow_start", timeout_seconds=timeout, start_node=entry_node,
          max_tool_rounds=max_iterations)
    try:
        if resuming:
            if stop_after is not None:
                saved.expert_checkpoint["stop_after"] = stop_after
            state = runner.resume_checkpoint(saved, revise=revise is not None,
                                             advance_after_revision=advance_after_revision)
            if state.status != "completed":
                state = runner.run(prompt, state=state, stop_after=stop_after)
        elif start_node is None:
            state = runner.run(prompt, **({"stop_after": stop_after} if stop_after else {}))
        else:
            run_options = {"state": runner.start_from_project(start_node, prompt, saved)}
            if stop_after:
                run_options["stop_after"] = stop_after
            state = runner.run(prompt, **run_options)
    except Exception as exc:
        progress["status"] = "failed"
        event("workflow_error", error=str(exc))
        raise
    _write_json(project / "workflow-state.json", state.to_dict())
    progress["status"] = state.status
    progress["paper_status"] = state.paper_status
    event("workflow_end", node=state.current_node, error=state.error or "")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the paper workflow with live progress")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "expert"), default=None,
                        help="auto 默认自动运行；expert 在每个节点完成后暂停审阅")
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds",
                        type=int, default=100,
                        help="tool-enabled model rounds per phase-agent invocation (default: 100); not workflow stages")
    parser.add_argument("--max-experiment-repairs", type=int, default=3,
                        help="experiment repair limit across workflow resumes; 0 disables")
    parser.add_argument("--model", default=None,
                        help="model name; defaults to PAPER_AGENT_MODEL or gpt-4o")
    parser.add_argument("--timeout", type=float, default=300,
                        help="model request timeout in seconds (default: 300)")
    parser.add_argument("--paper-review-timeout", type=float, default=1800,
                        help="independent paper review timeout in seconds (default: 1800)")
    parser.add_argument("--paper-review-round2-timeout", type=float, default=1200,
                        help="round-two acceptance review timeout in seconds (default: 1200)")
    parser.add_argument("--improvement-start-round", type=int, choices=(1, 2), default=1,
                        help="resume improvement from a round; 2 preserves existing round-1 outputs")
    parser.add_argument("--start-node", help="start at a workflow node using verified project outputs")
    parser.add_argument("--stop-after", metavar="NODE", choices=WORKFLOW_NODE_NAMES,
                        help="stop after this workflow node completes")
    parser.add_argument("--single-node", metavar="NODE", choices=WORKFLOW_NODE_NAMES,
                        help="run one workflow node, including Runner preprocessing, then stop")
    parser.add_argument("--idea-max-total-gpu-hours", type=float, default=8.0,
                        help="total GPU-hour planning budget for idea pilots (default: 8)")
    parser.add_argument("--idea-search-paper-number", type=int, default=15,
                        help="target number of papers to retrieve for idea generation (default: 15)")
    parser.add_argument("prompt", nargs="*")
    args = parser.parse_args()
    if args.single_node and (args.start_node and args.start_node != args.single_node
                             or args.stop_after and args.stop_after != args.single_node):
        parser.error("--single-node must match --start-node and --stop-after when combined")
    if args.max_experiment_repairs < 0:
        parser.error("--max-experiment-repairs must be nonnegative")
    if args.timeout <= 0 or args.max_tool_rounds < 1:
        parser.error("--timeout and --max-tool-rounds must be positive")
    if not 0 < args.idea_max_total_gpu_hours < float("inf") or not 1 <= args.idea_search_paper_number <= 50:
        parser.error("idea GPU hours must be finite and positive; idea paper number must be 1-50")
    run_options = dict(max_iterations=args.max_tool_rounds, timeout=args.timeout,
                         model=args.model,
                         idea_config=IdeaNodeConfig(args.idea_max_total_gpu_hours,
                                                    args.idea_search_paper_number),
                         config=WorkflowConfig(start_node="literature",
                                               max_experiment_repairs=args.max_experiment_repairs,
                                               improvement_start_round=args.improvement_start_round,
                                               paper_review_timeout=args.paper_review_timeout,
                                               paper_review_round2_timeout=args.paper_review_round2_timeout))
    state = run_workflow(args.project, " ".join(args.prompt),
                         start_node=args.single_node or args.start_node,
                         stop_after=args.single_node or args.stop_after,
                         mode=args.mode,
                         **run_options)
    state = _interact_at_checkpoint(args.project, state, run_options)
    print(f"项目目录: {args.project.resolve()}")
    print(f"状态: {state.status}; 当前节点: {state.current_node}; 原因: {state.error}")
    if state.status not in {"completed", "awaiting_user"}:
        raise SystemExit({"awaiting_results": 2}.get(state.status, 1))


def _interact_at_checkpoint(project: Path, state: WorkflowState,
                            run_options: dict[str, Any]) -> WorkflowState:
    """Keep terminal sessions open; answers become file-based downstream input."""
    while state.status == "awaiting_user":
        checkpoint = state.expert_checkpoint
        print(f"\n{checkpoint['node']} 结果摘要：{checkpoint['summary']}")
        for number, question in enumerate(checkpoint["questions"], 1):
            print(f"{number}. {question}")
        print(f"可编辑报告：{project.resolve() / checkpoint['report']}")
        for relative in checkpoint.get("files", [])[1:]:
            print(f"相关产物：{project.resolve() / relative}")
        if not sys.stdin.isatty():
            print("当前无交互终端，已保存检查点。在终端重新运行 --project 项目目录 即可返回审阅。")
            break
        print("直接输入回答（可按题号一起回答），将据此修订报告正文后自动继续下一节点。\n"
              "直接回车表示接受当前报告，不修订并继续。\n"
              "也可先在编辑器直接修改报告，保存后回车，将按最新文件继续。Ctrl+C 保存退出。")
        try:
            answer = input("你的回答：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n检查点已保存，重新运行 --project 项目目录 即可返回审阅。")
            break
        report = project.resolve() / checkpoint["report"]
        if not report.is_file() or not report.read_text(encoding="utf-8").strip():
            print("报告缺失或为空，请先修复报告再回答或继续。")
            continue
        if answer:
            questions = "\n".join(f"{i}. {q}" for i, q in enumerate(checkpoint["questions"], 1))
            with report.open("a", encoding="utf-8") as stream:
                stream.write(f"\n\n## 专家审阅：用户确认的研究方向\n\n"
                             f"本次问题：\n{questions}\n\n用户回答：\n{answer}\n")
            print("正在根据你的回答修订当前节点产物，完成后自动继续……", flush=True)
            state = run_workflow(project, revise=(
                f"用户已回答 {checkpoint['node']} 的专家审阅问题，请据此修订当前节点产物。\n"
                f"主要文件：{json.dumps(checkpoint.get('files', [checkpoint['report']]), ensure_ascii=False)}。\n"
                "先读取现有报告，将用户明确选择整合到任务边界、研究范围、方法建议及验证方案；"
                "删除或改写与回答冲突的建议和待确认措辞，不要仅在末尾追加回答。"
                "保留有效文献证据及来源，不把用户偏好当作已验证的科学结论，"
                "未明确的事项保留为假设，不擅自声称已获得用户批准。"
                "按当前节点职责同步修订相关代码、图、正文或计划，满足原有产物校验。"
                "不得仅改实验报告冒充新实验结果，不得篡改执行回执；实验变更须经现有执行工具验证。"
                "保留用户回答以供追溯。此次修订完成后运行器会自动进入下游，无需再次提问确认。\n\n"
                f"本次问题：\n{questions}\n\n用户回答：\n{answer}"
            ), **run_options)
            if state.status != "awaiting_user":
                return state
        state = run_workflow(project, continue_run=True, **run_options)
    return state


if __name__ == "__main__":
    main()
