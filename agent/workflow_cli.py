"""Terminal entry point with live workflow progress and durable status files."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .loop import PhaseAgent
from .workflow import IdeaNodeConfig, WorkflowConfig, WorkflowRunner, WorkflowState

WORKFLOW_NODE_NAMES = (
    "literature", "ideas", "refine",
    "experiment_plan", "experiment_bridge",
    "paper_plan", "paper_figure", "paper_write",
    "paper_compile",
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


def run_workflow(project: Path, prompt: str, *, max_iterations: int = 12,
                 timeout: float = 300, start_node: str | None = None,
                 config: WorkflowConfig | None = None,
                 idea_config: IdeaNodeConfig | None = None,
                 stop_after: str | None = None,
                 model: str | None = None) -> WorkflowState:
    project = project.resolve()
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
        event("node_progress", node=node, round=details["round"], stage=details["stage"])

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
        if start_node is None:
            state = runner.run(prompt, **({"stop_after": stop_after} if stop_after else {}))
        else:
            state_path = project / "workflow-state.json"
            saved = WorkflowState.from_dict(json.loads(state_path.read_text(encoding="utf-8"))) if state_path.is_file() else None
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
    parser.add_argument("--max-tool-rounds", "--max-iterations", dest="max_tool_rounds",
                        type=int, default=12,
                        help="tool-enabled model rounds per phase-agent invocation (default: 12); not workflow stages; --max-iterations is the legacy alias")
    parser.add_argument("--max-experiment-repairs", type=int, default=3,
                        help="experiment repair limit across workflow resumes; 0 disables")
    parser.add_argument("--model", default=None,
                        help="model name; defaults to PAPER_AGENT_MODEL or gpt-4o")
    parser.add_argument("--timeout", type=float, default=300,
                        help="model request timeout in seconds (default: 300)")
    parser.add_argument("--start-node", help="start at a workflow node using verified project outputs")
    parser.add_argument("--single-node", metavar="NODE", choices=WORKFLOW_NODE_NAMES,
                        help="run one workflow node, including Runner preprocessing, then stop")
    parser.add_argument("--idea-max-total-gpu-hours", type=float, default=8.0,
                        help="total GPU-hour planning budget for idea pilots (default: 8)")
    parser.add_argument("--idea-search-paper-number", type=int, default=15,
                        help="target number of papers to retrieve for idea generation (default: 15)")
    parser.add_argument("prompt", nargs="+")
    args = parser.parse_args()
    if args.max_experiment_repairs < 0:
        parser.error("--max-experiment-repairs must be nonnegative")
    if args.timeout <= 0 or args.max_tool_rounds < 1:
        parser.error("--timeout and --max-tool-rounds must be positive")
    if not 0 < args.idea_max_total_gpu_hours < float("inf") or not 1 <= args.idea_search_paper_number <= 50:
        parser.error("idea GPU hours must be finite and positive; idea paper number must be 1-50")
    state = run_workflow(args.project, " ".join(args.prompt),
                         max_iterations=args.max_tool_rounds, timeout=args.timeout,
                         start_node=args.start_node,
                         stop_after=args.single_node,
                         model=args.model,
                         idea_config=IdeaNodeConfig(args.idea_max_total_gpu_hours,
                                                    args.idea_search_paper_number),
                         config=WorkflowConfig(start_node="literature",
                                               max_experiment_repairs=args.max_experiment_repairs))
    print(f"项目目录: {args.project.resolve()}")
    print(f"状态: {state.status}; 当前节点: {state.current_node}; 原因: {state.error}")
    if state.status != "completed":
        raise SystemExit({"awaiting_results": 2}.get(state.status, 1))


if __name__ == "__main__":
    main()
