"""Workflow entry points, single-stage execution and checkpoint interaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .loop import PhaseAgent
from .workflow import WorkflowConfig, WorkflowRunner, WorkflowState
from .stages import BY_NODE, NEXT_NODE, WORKFLOW_NODE_NAMES
from batteryscientist.storage import write_json as _write_json
from batteryscientist.storage import read_json_or_default as read_json


def checkpoint_author(project: Path, node: str) -> str:
    """Keep the recorded stage author when regenerating a pending checkpoint."""
    state = read_json(Path(project) / 'workflow-state.json', {})
    role = state.get('node_results', {}).get(node, {}).get('role')
    return role if role in {'idea_generator', 'experiment', 'paper_writer'} else BY_NODE[node].role


def is_approval(value):
    """Accept standalone approval replies, not questions or qualified consent."""
    value = re.sub(r'[\s，,。！!；;、]+', '', value)
    return bool(re.fullmatch(
        r'(?:(?:批准|同意)(?:(?:当前|推荐|这个|该)?方案)?'
        r'(?:并?(?:继续|开始|进入)(?:运行|执行|实验规划|下一步|下一阶段)?)?'
        r'|可以的?(?:开始|继续)(?:运行|执行)?'
        r'|(?:都)?按(?:你|您)的(?:建议|意见|想法)(?:来|继续|执行))', value))


def is_review_approval(answer, questions):
    """Recognize consent in either free text or the web's question/answer form."""
    if is_approval(answer):
        return True
    entries = answer.split('\n\n问题：')
    if not questions or len(entries) != len(questions):
        return False
    for index, (entry, question) in enumerate(zip(entries, questions)):
        prefix = ('问题：' if index == 0 else '') + question + '\n回答：'
        if not entry.startswith(prefix) or not is_approval(entry[len(prefix):]):
            return False
    return True


def checkpoint_snapshot(project):
    project = Path(project).resolve()
    state_path = project / 'workflow-state.json'
    state = read_json(state_path, {})
    checkpoint = state.get('expert_checkpoint') or {}
    if state.get('status') != 'awaiting_user' or checkpoint.get('status') != 'pending':
        return None
    report = (project / checkpoint['report']).resolve()
    if not report.is_relative_to(project) or not report.is_file():
        raise ValueError('检查点报告不存在或位于项目之外')
    files = set(checkpoint.get('files', [checkpoint['report']]))
    return {'state_sha256': hashlib.sha256(state_path.read_bytes()).hexdigest(),
            'report': checkpoint['report'],
            'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
            'files': {name: hashlib.sha256((project / name).read_bytes()).hexdigest()
                      for name in files
                      if (project / name).resolve().is_relative_to(project) and (project / name).is_file()}}


def approve_checkpoint(project, expected, *, stop_after=None, proposal_review_sha=None):
    """Use the workflow's approval validator while excluding active stage writes."""
    import fcntl
    from types import SimpleNamespace
    from batteryscientist.agent.runtime.herdr import ensure_no_pending, directory
    from batteryscientist.agent.runtime.execution import project_write_lock
    from batteryscientist.agent.workflow import WorkflowRunner, WorkflowState
    project = Path(project).resolve()
    root = directory(project)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'transport.lock').open('a') as transport, project_write_lock(project):
        fcntl.flock(transport, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ensure_no_pending(project)
        if expected is None:
            raise ValueError('没有可批准的待审版本')
        if 'files' not in expected:
            raise ValueError('终端代码已更新，请先重启 Main，查看新版产物后再批准')
        if checkpoint_snapshot(project) != expected:
            raise ValueError('方案或检查点已变化，本次未批准；请查看最新方案后再确认')
        state = WorkflowState.from_dict(read_json(project / 'workflow-state.json', {}))
        node = state.current_node
        # Approval respects the user's overall workflow boundary.
        stop_after = stop_after or state.workflow_stop_after
        if node == 'refine' and stop_after is None:
            stop_after = 'experiment_plan'  # Proposal approval authorizes planning.
        state.expert_checkpoint['stop_after'] = stop_after
        state.workflow_stop_after = stop_after
        state = WorkflowRunner(SimpleNamespace(project=project)).resume_checkpoint(
            state, proposal_review_sha=proposal_review_sha)
        _write_json(project / 'workflow-state.json', state.to_dict())
        return state, stop_after


def execute_stage(project: Path, node: str, instruction: str, *, role: str, project_goal: str,
                  max_iterations: int = 300, timeout: float = 300,
                  config: WorkflowConfig | None = None,
                  mode: str | None = None, revise: str | None = None,
                  proposal_review_sha: str | None = None, model: str | None = None,
                  execution: dict | None = None, workflow_stop_after: str | None = None,
                  approve_revision: bool = False, full_stage: bool = True,
                  paper_review_timeout: float | None = None,
                  paper_review_round2_timeout: float | None = None,
                  on_event: Callable[[dict[str, Any]], None] | None = None) -> WorkflowState:
    """Execute main's single-node assignment and persist its outcome without routing."""
    if node not in WORKFLOW_NODE_NAMES:
        raise ValueError("unknown workflow node")
    if full_stage and not project_goal.strip():
        raise ValueError("project_goal must not be empty")
    if revise is not None and not revise.strip():
        raise ValueError("审阅修改意见不能为空")
    project = project.resolve()
    state_path = project / "workflow-state.json"
    saved = WorkflowState.from_dict(json.loads(state_path.read_text())) if state_path.is_file() else None
    if revise is not None and not full_stage:
        raise ValueError("检查点重跑必须使用完整阶段派单")
    if revise is not None:
        if saved is None or saved.status != "awaiting_user":
            raise ValueError("项目没有等待用户确认的专家检查点")
        if saved.current_node != node:
            raise ValueError("Revision must match the pending checkpoint node")
        if mode is not None:
            raise ValueError("修订不能同时修改 mode")
    elif full_stage and saved and saved.status == "awaiting_user":
        raise ValueError("项目存在待审阅检查点，请先批准或修订")
    config = config or WorkflowConfig()
    if paper_review_timeout is not None:
        config = replace(config, paper_review_timeout=paper_review_timeout)
    if paper_review_round2_timeout is not None:
        config = replace(config, paper_review_round2_timeout=paper_review_round2_timeout)
    if (node == "improvement" and saved is not None
            and saved.status in {"failed", "running", "interrupted"}
            and saved.improvement_checkpoint):
        config = replace(config, improvement_resume=True)
    if mode is not None:
        config = replace(config, mode=mode)
        if saved is not None:
            saved.mode = mode
    started = time.monotonic()
    progress_path = project / "workflow-progress.json"
    progress: dict[str, Any] = {"status": "running", "current_node": node,
                                "recent_events": []}

    def event(kind: str, **details: Any) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {"time": now, "kind": kind, **details}
        if kind == 'assistant_delta':
            if on_event is not None:
                on_event(record)
            return
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

    def on_node_start(node: str, state: WorkflowState) -> None:
        progress["run_id"] = state.run_id
        progress["status"] = state.status
        progress["paper_status"] = state.paper_status
        progress.update(successful_node_executions=state.transitions,
                        fault_retries=state.fault_retry_count)
        _write_json(project / "workflow-state.json", state.to_dict())
        progress.pop("tool", None)
        progress.pop("iteration", None)
        event("node_start", node=node)

    def on_node_end(node: str, state: WorkflowState, result: Any) -> None:
        progress["status"] = state.status
        progress["paper_status"] = state.paper_status
        progress.update(successful_node_executions=state.transitions,
                        fault_retries=state.fault_retry_count)
        _write_json(project / "workflow-state.json", state.to_dict())
        event("node_end", node=node, files=result.files)

    def on_node_progress(node: str, state: WorkflowState, details: dict[str, Any]) -> None:
        progress["paper_status"] = state.paper_status
        _write_json(project / "workflow-state.json", state.to_dict())
        progress["improvement"] = details if node == "improvement" else progress.get("improvement", {})
        event("node_progress", node=node, **details)

    if not full_stage:
        from types import SimpleNamespace
        from .stages import BY_NODE
        from batteryscientist.project_config import resolve_mode

        saved_review = read_json(project / 'workflow-state.json', {})
        watched = {ref['path'] for refs in saved_review.get('artifacts', {}).values() for ref in refs}
        checkpoint = saved_review.get('expert_checkpoint') or {}
        watched.update(checkpoint.get('files', [checkpoint['report']] if checkpoint.get('report') else []))

        def snapshot():
            return {name: (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                    for name in watched
                    if (path := (project / name).resolve()).is_relative_to(project)}

        before = snapshot()
        agent = PhaseAgent(project, model=model, max_iterations=max_iterations, timeout=timeout,
                           assignment_role=role, full_stage=False,
                           interaction_mode=resolve_mode(project, mode),
                           on_progress=lambda kind, data: event(kind, **data))
        progress['full_stage'] = False
        try:
            try:
                result = agent.go(
                    f"项目总目标（背景）：\n{project_goal}\n\n本次任务：\n{instruction}",
                    phase=BY_NODE[node].phase)
            finally:
                # A failed or cancelled task can leave useful partial edits.
                # Those edits must not retain approval of the previous version.
                after = snapshot()
                files = sorted(name for name, digest in after.items() if before.get(name) != digest)
                declared = list(getattr(agent.last_state, 'files', []))
                pending = (refresh_revision_checkpoint(project, node, project_goal, files=declared)
                           if before != after or any(name not in watched for name in declared) else False)
            status = 'awaiting_user' if pending else 'completed'
            progress['status'] = status
            event('workflow_end', node=node, status=status)
            return SimpleNamespace(status=status, error=None, final=result.final,
                                   files=list(dict.fromkeys([*result.files, *files])))
        except BaseException as exc:
            progress['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
            event('assignment_error', node=node, error=str(exc))
            raise

    agent = PhaseAgent(project, model=model, max_iterations=max_iterations, timeout=timeout,
                       assignment_role=role,
                       on_progress=lambda kind, data: event(kind, **data))
    runner = WorkflowRunner(agent, config=config,
                            on_node_start=on_node_start, on_node_end=on_node_end,
                            on_node_progress=on_node_progress)
    event("workflow_start", timeout_seconds=timeout, start_node=node,
          max_tool_rounds=max_iterations)
    try:
        if revise is not None:
            if saved.project_goal != project_goal:
                raise ValueError("项目总目标不能由阶段指令覆盖；请单独传入 instruction")
            state = runner.resume_checkpoint(saved, revise=True,
                                             proposal_review_sha=proposal_review_sha)
            instruction = revise
        else:
            state = runner.start_from_project(node, project_goal, saved)
        state.workflow_stop_after = workflow_stop_after
        state = runner.execute_node(node, project_goal, state=state, instruction=instruction,
                                    execution=execution,
                                    approve_revision=revise is not None and approve_revision)
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        progress["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        event("workflow_error", error=str(exc))
        raise
    _write_json(project / "workflow-state.json", state.to_dict())
    progress["status"] = state.status
    progress["paper_status"] = state.paper_status
    event("workflow_end", node=state.current_node, error=state.error or "")
    return state


def refresh_revision_checkpoint(project, node, project_goal=None, *, files=()):
    saved = read_json(project / 'workflow-state.json', {})
    if saved.get('mode', 'auto') == 'auto':
        return False
    checkpoint = saved.get('expert_checkpoint') or {}
    was_approved = any(item.get('event') == 'expert_approved' and item.get('node') == node
                       for item in saved.get('history', []))
    supersedes_pending = (saved.get('status') == 'awaiting_user'
                          and checkpoint.get('node') in WORKFLOW_NODE_NAMES
                          and WORKFLOW_NODE_NAMES.index(node) < WORKFLOW_NODE_NAMES.index(checkpoint['node']))
    if not (node == 'refine' or checkpoint.get('node') == node or was_approved or supersedes_pending):
        return False
    prior = (checkpoint.get('files', [checkpoint['report']]) if checkpoint.get('node') == node
             else [ref['path'] for ref in saved.get('artifacts', {}).get(node, [])])
    files = list(dict.fromkeys([*prior, *files]))
    if not files:
        return False
    report = project / files[0]
    if not report.is_file():
        raise ValueError('修订后缺少检查点主产物')
    saved.update(status='awaiting_user', current_node=node, error=None)
    saved.setdefault('mode', 'auto')
    saved.setdefault('run_id', uuid4().hex)
    saved.setdefault('project_goal', project_goal or '')
    saved['expert_checkpoint'] = {
        'node': node, 'status': 'pending', 'report': files[0], 'files': files,
        'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
        'next_node': NEXT_NODE[node],
        'stop_after': checkpoint.get('stop_after', saved.get('workflow_stop_after')),
        'summary': '已按修改意见更新产物。请审阅新版后明确批准。',
        'questions': [], 'revision_id': uuid4().hex,
    }
    _write_json(project / 'workflow-state.json', saved)
    return True


def run_coordinated_workflow(project: Path, prompt: str = "", *, start_node=None,
                             stop_after=None, mode=None, continue_run=False, revise=None, resume_run=False,
                             proposal_review_sha=None,
                             model=None, max_iterations=300, timeout=300, on_event=None,
                             execution_guard=None, wait_for_user=False, **workflow_options):
    """All user-facing advancement goes through main; runner only supplies stages."""
    from .coordinator import CoordinatorAgent

    project = project.resolve()
    if stop_after is not None and stop_after not in WORKFLOW_NODE_NAMES:
        raise ValueError("unknown workflow node")
    path = project / "workflow-state.json"
    saved = WorkflowState.from_dict(json.loads(path.read_text())) if path.is_file() else None
    if continue_run and revise is not None:
        raise ValueError("继续与修订不能同时使用")
    if continue_run or revise is not None:
        if start_node or mode is not None or prompt.strip():
            raise ValueError("审批/修订不能与入口节点、mode 或新任务 prompt 组合")
        if saved is None or saved.status != "awaiting_user":
            raise ValueError("项目没有等待用户确认的专家检查点")
        if revise is not None and not revise.strip():
            raise ValueError("审阅修改意见不能为空")
        if continue_run:
            saved, stop_after = approve_checkpoint(
                project, checkpoint_snapshot(project), stop_after=stop_after,
                proposal_review_sha=proposal_review_sha)
            if saved.status == "completed":
                if saved.current_node == 'improvement':
                    CoordinatorAgent(project, model=model, timeout=timeout, mode=saved.mode,
                                     on_event=on_event, wait_for_user=wait_for_user).go(
                        '最终修订产物已正式批准，完整研究已完成。调用 curate_wiki(project_evidence=true) 沉淀成果后总结。',
                        archive_completed=True)
                return saved
        start_node = saved.current_node
        prompt = revise if revise is not None else saved.project_goal
    elif saved and saved.status == "awaiting_user":
        if not prompt.strip() and start_node is None:
            return saved
        raise ValueError("项目存在待审阅检查点，请先批准或修订")
    if not prompt.strip():
        prompt = saved.project_goal if saved else ""
    if not prompt.strip():
        raise ValueError("首次运行需要任务 prompt")
    if resume_run:
        if saved is None:
            raise ValueError("项目没有可恢复的流程状态")
        start_node = start_node or saved.failed_node or saved.current_node
    if saved and (resume_run or continue_run or revise is not None):
        stop_after = stop_after or saved.workflow_stop_after
    node = start_node or (workflow_options.get("config") or WorkflowConfig()).start_node
    if node not in WORKFLOW_NODE_NAMES or (stop_after and stop_after not in WORKFLOW_NODE_NAMES):
        raise ValueError("unknown workflow node")
    mode = mode or (saved.mode if saved else "auto")
    if revise is not None:
        # Feedback is a main conversation, not an implicit role assignment or
        # approval. Main can edit MD itself or delegate a substantive revision.
        agent = CoordinatorAgent(project, model=model, worker_max_iterations=max_iterations,
                                 timeout=timeout, mode=mode, on_event=on_event,
                                 execution_guard=execution_guard, stop_after=stop_after,
                                 workflow_options=workflow_options, project_goal=saved.project_goal,
                                 wait_for_user=wait_for_user)
        result = agent.go(f'用户要求修改当前待审的 {node} 产物，修改意见不代表批准。'
                         f'先读取产物，理解回答，直接落实Markdown修改；需要研究或实现时再委派角色。\n{revise}')
        state = WorkflowState.from_dict(json.loads(path.read_text()))
        state.status, state.error = result.status, result.error
        return state
    task = None
    if node == stop_after:
        task = {"node": node, "role": BY_NODE[node].role, "full_stage": True,
                "instruction": (f"完成 {node} 阶段并交付该阶段产物，验收后停止。"
                                if saved is None or continue_run or resume_run else prompt)}
    agent = CoordinatorAgent(project, model=model, worker_max_iterations=max_iterations,
                             timeout=timeout, mode=mode, on_event=on_event,
                             execution_guard=execution_guard, stage_task=task,
                             stop_after=stop_after,
                             workflow_options=workflow_options, first_node=node,
                             project_goal=saved.project_goal if saved else prompt,
                             wait_for_user=wait_for_user)
    result = agent.go(prompt)
    state = WorkflowState.from_dict(json.loads(path.read_text())) if path.is_file() else WorkflowState("main")
    state.status, state.error = result.status, result.error
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the paper workflow with live progress")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "expert"), default=None,
                        help="auto 默认自动运行；expert 在每个节点完成后暂停审阅")
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds",
                        type=int, default=300,
                        help="progress reporting interval in model rounds (default: 300); unfinished assignments continue automatically")
    parser.add_argument("--model", default=None,
                        help="model name; defaults to BATTERYSCIENTIST_MODEL or gpt-4o")
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
    parser.add_argument("prompt", nargs="*")
    args = parser.parse_args()
    if args.single_node and (args.start_node and args.start_node != args.single_node
                             or args.stop_after and args.stop_after != args.single_node):
        parser.error("--single-node must match --start-node and --stop-after when combined")
    if args.timeout <= 0 or args.max_tool_rounds < 1:
        parser.error("--timeout and --max-tool-rounds must be positive")
    def print_progress(record):
        if record['kind'] == 'assistant_delta':
            return
        label = record.get('node') or record.get('phase') or 'main'
        error = f" error={record['error']}" if record.get('error') else ''
        print(f"[{record['time']}] {label}: {record['kind']}{error}", flush=True)

    run_options = dict(max_iterations=args.max_tool_rounds, timeout=args.timeout,
                         on_event=print_progress,
                         model=args.model,
                         config=WorkflowConfig(start_node="literature",
                                               improvement_start_round=args.improvement_start_round,
                                               paper_review_timeout=args.paper_review_timeout,
                                               paper_review_round2_timeout=args.paper_review_round2_timeout))
    state = run_coordinated_workflow(args.project, " ".join(args.prompt),
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
            if checkpoint.get("decision_options"):
                print("   " + " / ".join(checkpoint["decision_options"][number - 1]))
        print(f"可编辑报告：{project.resolve() / checkpoint['report']}")
        for relative in checkpoint.get("files", [])[1:]:
            print(f"相关产物：{project.resolve() / relative}")
        if not sys.stdin.isatty():
            print("当前无交互终端，已保存检查点。在终端重新运行 --project 项目目录 即可返回审阅。")
            break
        print("输入回答或修改意见，由主控落实到正文；新版展示后再批准。\n" +
              "输入“批准”或直接回车表示接受当前报告，不修订并继续。\n"
              "也可先在编辑器直接修改报告，保存后回车，将按最新文件继续。Ctrl+C 保存退出。")
        try:
            answer = input("你的回答：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n检查点已保存，重新运行 --project 项目目录 即可返回审阅。")
            break
        report = project.resolve() / checkpoint["report"]
        if not report.is_file():
            print("报告缺失或为空，请先修复报告再回答或继续。")
            continue
        if answer and not is_review_approval(answer, checkpoint['questions']):
            questions = "\n".join(f"{i}. {q}" for i, q in enumerate(checkpoint["questions"], 1))
            print("正在根据你的回答修订当前节点产物……", flush=True)
            state = run_coordinated_workflow(project, revise=(
                f"用户已回答 {checkpoint['node']} 的专家审阅问题，请据此修订当前节点产物。\n"
                f"主要文件：{json.dumps(checkpoint.get('files', [checkpoint['report']]), ensure_ascii=False)}。\n"
                "读取现有产物并落实用户回答，保留有效证据和回答记录；修改完成后交回审阅，不视为批准。\n\n"
                f"本次问题：\n{questions}\n\n用户回答：\n{answer}"
            ), **run_options)
            if state.status != "awaiting_user":
                return state
            continue
        state = run_coordinated_workflow(project, continue_run=True, **run_options)
    return state


if __name__ == "__main__":
    main()
