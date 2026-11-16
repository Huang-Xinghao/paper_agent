"""Deterministic workflow controller for research and paper production."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import math
from functools import wraps
from paper_agent.tools.experiments.queue import run_queue
from paper_agent.tools.experiments.executor import ExperimentExecutor, AwaitingResults, ExperimentExecutionError, receipt_matches_job, job_dependencies
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .loop import ArtifactValidationError, PhaseAgent
from .stages import BY_NODE, NEXT_NODE
from paper_agent.tools import ToolContext, ToolResult, ToolSpec
from paper_agent.tools.experiments.plan import EXPERIMENT_INTERFACE, read_plan_blocks, write_experiment_tracker
from paper_agent.tools.paper_compile import compile_pdf
from paper_agent.storage import write_json as _write_json


WorkflowStatus = Literal["running", "awaiting_results", "awaiting_user", "completed", "failed"]
PaperStatus = Literal["pending", "pdf_ready"]


def artifact_contract(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ArtifactValidationError(str(exc)) from exc
    return checked


@dataclass
class WorkflowState:
    """Serializable state passed between workflow nodes."""

    run_id: str
    project_goal: str = ""
    mode: Literal["auto", "expert"] = "auto"
    expert_checkpoint: dict[str, Any] | None = None
    current_node: str = "literature"
    status: WorkflowStatus = "running"
    paper_status: PaperStatus = "pending"
    workflow_stop_after: str | None = None
    artifacts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    transitions: int = 0  # Lifetime successful node executions; never a run budget.
    fault_retry_count: int = 0
    failed_node: str | None = None
    error: str | None = None
    node_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    improvement_checkpoint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        value = dict(value)
        for key in ('experiment_completed_receipts', 'experiment_repair_count', 'experiment_feedback'):
            value.pop(key, None)
        return cls(**value)


@dataclass(frozen=True)
class WorkflowConfig:
    """Routing and retry limits for the research-to-paper workflow."""

    start_node: str = "literature"
    max_node_steps_per_run: int | None = None
    improvement_start_round: int = 1
    paper_review_timeout: float = 1800
    paper_review_round2_timeout: float = 1200
    improvement_resume: bool = False
    mode: Literal["auto", "expert"] = "auto"


@dataclass(frozen=True)
class IdeaNodeConfig:
    max_total_gpu_hours: float = 8.0


@dataclass
class NodeResult:
    """Output returned by a workflow node handler."""

    artifact: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowRunner:
    """Route research stages through role sessions hosted by :class:`PhaseAgent`.

    Nodes are ordinary Python methods with explicit, testable transitions.
    """

    def __init__(self, agent: PhaseAgent, *, config: WorkflowConfig | None = None,
                 idea_config: IdeaNodeConfig | None = None,
                 run_id: str | None = None,
                 on_node_start: Callable[[str, WorkflowState], None] | None = None,
                 on_node_end: Callable[[str, WorkflowState, NodeResult], None] | None = None,
                 on_node_progress: Callable[[str, WorkflowState, dict[str, Any]], None] | None = None):
        self.agent = agent
        self.experiments = ExperimentExecutor(agent.project, knowledge_root=getattr(agent, "knowledge_root", None))
        self.config = config or WorkflowConfig()
        if self.config.mode not in {"auto", "expert"}:
            raise ValueError("mode must be auto or expert")
        self.idea_config = idea_config or IdeaNodeConfig()
        if not math.isfinite(self.idea_config.max_total_gpu_hours) or self.idea_config.max_total_gpu_hours <= 0:
            raise ValueError("max_total_gpu_hours must be a finite positive number")
        if self.config.max_node_steps_per_run is not None and self.config.max_node_steps_per_run < 1:
            raise ValueError("max_node_steps_per_run must be positive")
        if self.config.improvement_start_round not in (1, 2):
            raise ValueError("improvement_start_round must be 1 or 2")
        for name in ("paper_review_timeout", "paper_review_round2_timeout"):
            if not math.isfinite(getattr(self.config, name)) or getattr(self.config, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self.run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
        self.on_node_start = on_node_start
        self.on_node_end = on_node_end
        self.on_node_progress = on_node_progress
        self._nodes: dict[str, Callable[[WorkflowState, str], NodeResult]] = {
            "literature": self._run_literature,
            "data_search": self._run_data_search,
            "ideas": self._run_ideas,
            "refine": self._run_refine,
            "experiment_plan": self._run_experiment_plan,
            "experiment_bridge": self._run_experiment_bridge,
            "paper_plan": self._run_paper_plan,
            "paper_figure": self._run_paper_figure,
            "paper_write": self._run_paper_write,
            "paper_compile": self._run_paper_compile,
            "improvement": self._run_improvement,
        }

    def run(self, prompt: str, *, state: WorkflowState | None = None,
            stop_after: str | None = None, instruction: str = "") -> WorkflowState:
        """Run until a terminal state and return the complete workflow state."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        state = state or WorkflowState(self.run_id, current_node=self.config.start_node,
                                       project_goal=prompt, mode=self.config.mode)
        if state.project_goal and state.project_goal != prompt:
            raise ValueError("项目总目标不能由阶段指令覆盖；请单独传入 instruction")
        state.project_goal = prompt
        instruction_node = state.current_node
        if state.status == "awaiting_results":
            state.status = "running"
            state.error = None
        if state.status != "running":
            return state
        self.agent.interaction_mode = state.mode

        # Guard routing loops within this invocation; prior runs cost no budget.
        steps_this_run = 0
        # Experiment repair/expansion now happens inside the bridge tool loop.
        step_limit = self.config.max_node_steps_per_run or (
            len(self._nodes) + 6)
        while state.status == "running":
            if steps_this_run >= step_limit:
                return self._fail(state, "workflow exceeded max_node_steps_per_run")
            handler = self._nodes.get(state.current_node)
            if handler is None:
                return self._fail(state, f"unknown workflow node: {state.current_node}")
            node = state.current_node
            steps_this_run += 1
            if state.failed_node is not None:
                state.fault_retry_count += 1
                state.failed_node = None
            try:
                if self.on_node_start:
                    self.on_node_start(node, state)
                node_prompt = state.project_goal
                instructions = instruction if node == instruction_node else ""
                if instructions:
                    node_prompt = f"项目总目标：\n{state.project_goal}\n\n当前阶段指令（仅适用于 {instruction_node}）：\n{instructions}"
                review_required = state.mode == "expert"
                result = handler(state, node_prompt)
                selected_report = (self._review_files(node)[0]
                                   if state.mode == 'auto' and node in {'refine', 'experiment_plan'} else None)
                selected_sha = (hashlib.sha256(self._require_file(selected_report).read_bytes()).hexdigest()
                                if selected_report else None)
                if review_required:
                    checkpoint = {'summary': '阶段产物已生成，请审阅。',
                                  'questions': []}
                    report = self._require_file(self._review_files(node)[0])
                    state.expert_checkpoint = {
                        **checkpoint, "node": node, "next_node": self._next_node(node),
                        "report": self._review_files(node)[0],
                        "files": self._review_files(node), "status": "pending",
                        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                        "stop_after": stop_after,
                    }
            except AwaitingResults as exc:
                state.status = "awaiting_results"
                state.error = str(exc)
                return state
            except Exception as exc:
                state.failed_node = node
                return self._fail(state, f"node {node} failed: {exc}")
            state.artifacts[node] = self._artifact_refs(node)
            state.node_results[node] = {
                **result.metadata,
                "instruction": instructions,
            }
            messages = getattr(self.agent, "last_state", None)
            if messages is not None:
                for key in ("role", "session_id", "task_id"):
                    value = getattr(messages, key, None)
                    if value is not None:
                        state.node_results[node][key] = value
            state.history.append({
                "node": node,
                "metadata": result.metadata,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if selected_sha is not None:
                # Keep an explicit, versioned execution basis without inventing
                # a user approval. Later edits must go through their owning stage.
                state.history.append({
                    'event': 'auto_selected', 'node': node,
                    'report': selected_report,
                    'sha256': selected_sha,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                })
            state.transitions += 1
            if node in {"paper_compile", "improvement"}:
                state.paper_status = "pdf_ready"
            if review_required:
                state.status = "awaiting_user"
                if self.on_node_end:
                    self.on_node_end(node, state, result)
                return state
            if stop_after == node:
                state.status = "completed"
                state.current_node = node
                if self.on_node_end:
                    self.on_node_end(node, state, result)
                return state
            try:
                next_node = self._next_node(node)
            except Exception as exc:
                return self._fail(state, f"routing after {node} failed: {exc}")
            if next_node is None and state.status == "running":
                state.status = "completed"
            elif next_node is not None:
                state.current_node = next_node
            if self.on_node_end:
                self.on_node_end(node, state, result)
        return state

    def resume_checkpoint(self, state: WorkflowState, *, revise: bool = False,
                          proposal_review_sha: str | None = None) -> WorkflowState:
        """Approve current files, or prepare to revise the paused node."""
        checkpoint = state.expert_checkpoint
        if (state.status != "awaiting_user" or state.current_node not in self._nodes
                or not checkpoint or checkpoint.get("status") != "pending"
                or checkpoint.get("node") != state.current_node):
            raise ValueError("没有待确认的专家检查点")
        proposal_review = state.current_node == 'refine'
        if revise and proposal_review:
            from paper_agent.proposal_review import require_resolved_proposal_review
            require_resolved_proposal_review(self._project)
        if not revise:
            node = state.current_node
            review_receipt = {}
            if proposal_review:
                from paper_agent.proposal_review import require_resolved_proposal_review, require_candidate
                if proposal_review_sha:
                    require_candidate(self._project, proposal_review_sha)
                else:
                    require_resolved_proposal_review(self._project)
            for relative in self._review_files(node):
                path = self._require_file(relative)
                if path.suffix != ".pdf" and not path.read_text(encoding="utf-8").strip():
                    raise ValueError(f"节点产物不能为空：{relative}")
            if node == "experiment_plan":
                write_experiment_tracker(self._project, read_plan_blocks(self._project))
            if node in {"paper_compile", "improvement"}:
                self._compile_pdf()
            if proposal_review_sha:
                if not proposal_review:
                    raise ValueError("方案审阅批准只能用于研究精炼的专家检查点")
                from paper_agent.proposal_review import publish_candidate
                review_receipt = publish_candidate(self._project, proposal_review_sha)
            report = self._require_file(checkpoint["report"])
            content = report.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("节点产物不能为空，请编辑后再确认")
            state.artifacts[node] = self._artifact_refs(node)
            checkpoint["confirmed_files"] = {
                relative: hashlib.sha256(self._require_file(relative).read_bytes()).hexdigest()
                for relative in self._review_files(node)
            }
            checkpoint["confirmed_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
            checkpoint["status"] = "approved"
            state.history.append({
                "event": "expert_approved", "node": node,
                "report": checkpoint["report"], "sha256": checkpoint["confirmed_sha256"],
                **review_receipt,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            next_node = checkpoint["next_node"]
            if checkpoint.get("stop_after") == node or next_node is None:
                state.status = "completed"
            else:
                state.current_node = next_node
        else:
            checkpoint["status"] = "revision_requested"
            # 最终方案的修改意见只授权修订，新稿仍需人明确批准。
        if state.status != "completed":
            state.status = "running"
        state.error = None
        return state


    def _go(self, prompt: str, *, phase: str,
            completion_check: Callable[[], None] | None = None):
        check = completion_check or (lambda: self._validate_phase_outputs(phase))
        return self.agent.go(prompt, phase=phase, completion_check=check)

    @staticmethod
    def _review_files(node: str) -> list[str]:
        return list(BY_NODE[node].review_paths)

    def _validate_phase_outputs(self, phase: str) -> None:
        from .stages import validate_outputs
        try:
            validate_outputs(self._project, phase)
        except ValueError as exc:
            raise ArtifactValidationError(str(exc)) from exc

    def start_from_project(self, node: str, prompt: str,
                           state: WorkflowState | None = None) -> WorkflowState:
        """Prepare an explicit entry point from durable outputs, checking its inputs."""
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        if state is None:
            state = WorkflowState(self.run_id, project_goal=prompt, mode=self.config.mode)
        for relative in BY_NODE[node].inputs:
            self._require_file(relative)
        if node == "refine":
            state.artifacts["ideas"] = self._artifact_refs("ideas")
        if state.project_goal and state.project_goal != prompt:
            raise ValueError("项目总目标不能由阶段指令覆盖；请单独传入 instruction")
        state.project_goal = prompt
        state.current_node = node
        state.expert_checkpoint = None
        state.status = "running"
        state.error = None
        return state

    def _run_data_search(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Select and understand datasets before proposing research ideas."""
        literature_path = "literature/RESEARCH_REPORT.md"
        self._validate_required_artifacts([literature_path])
        result = self._go(
            f"{prompt}\n\n"
            "文献报告路径：literature/RESEARCH_REPORT.md。\n"
            "将用于生成研究想法的数据报告写入 data-search/DATA_REPORT.md。",
            phase="data_search",
        )
        self._validate_phase_outputs("data_search")
        report_path = "data-search/DATA_REPORT.md"
        report = self._require_file(report_path).read_text(encoding="utf-8")
        return NodeResult(report_path, {"phase": "data_search", "iterations": result.iterations,
                                       "canonical_path": report_path,
                                       "literature_source": literature_path,
                                       "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest()})

    def _run_ideas(self, state: WorkflowState, prompt: str, *,
                   max_total_gpu_hours: float | None = None) -> NodeResult:
        max_total_gpu_hours = (self.idea_config.max_total_gpu_hours if max_total_gpu_hours is None
                               else max_total_gpu_hours)
        if not math.isfinite(max_total_gpu_hours) or max_total_gpu_hours <= 0:
            raise ValueError("max_total_gpu_hours must be a finite positive number")
        literature_path = "literature/RESEARCH_REPORT.md"
        literature = self._require_file(literature_path).read_text(encoding="utf-8")
        if not literature.strip():
            raise ValueError(f"{literature_path} is empty")
        data_path = "data-search/DATA_REPORT.md"
        data_report = self._require_file(data_path).read_text(encoding="utf-8")
        if not data_report.strip():
            raise ValueError(f"{data_path} is empty")
        instruction = (
            "生成具体、可证伪的研究想法，并保存为项目产物。"
            "撰写完整、可独立阅读的报告，不描述生成或修改过程。"
        )
        result = self._go(
            f"{prompt}\n\n当前节点任务：\n{instruction}\n"
            f"研究想法节点参数：\nMAX_TOTAL_GPU_HOURS={max_total_gpu_hours:g}. "
            "GPU 预算是所有拟议预实验的总估算值，不代表获准执行这些实验。\n\n"
            "复用 literature/RESEARCH_REPORT.md 和 data-search/DATA_REPORT.md。"
            "优先使用 Research Wiki 和数据资源补充具体证据缺口；仅对高度相关且 Wiki 缺少关键细节的论文定向核对原文。",
            phase="ideas",
        )
        idea = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if not idea.strip():
            raise ValueError("idea-stage/IDEA_REPORT.md is empty")
        return NodeResult("idea-stage/IDEA_REPORT.md", {"phase": "ideas", "iterations": result.iterations,
                                        "canonical_path": "idea-stage/IDEA_REPORT.md",
                                        "literature_source": literature_path,
                                        "literature_sha256": hashlib.sha256(literature.encode("utf-8")).hexdigest(),
                                        "data_source": data_path,
                                        "data_sha256": hashlib.sha256(data_report.encode("utf-8")).hexdigest(),
                                        "idea_parameters": {
                                            "max_total_gpu_hours": max_total_gpu_hours}})


    def _run_literature(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Let the role collect and interpret literature under its phase skill."""
        result = self._go(
            "研究任务：\n" + prompt + "\n将文献报告写入 literature/RESEARCH_REPORT.md。",
            phase="literature",
        )
        self._require_file("literature/RESEARCH_REPORT.md")
        return NodeResult("literature/RESEARCH_REPORT.md", {
            "phase": "literature", "iterations": result.iterations,
        })

    def _run_refine(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the generated idea into the final proposal."""
        result = self._go(
            f"""{prompt}

使用以下输入细化已生成的研究想法。
研究想法产物：idea-stage/IDEA_REPORT.md
""",
            phase="research_refine",
        )
        self._require_file("refine-logs/FINAL_PROPOSAL.md")
        return NodeResult("refine-logs/FINAL_PROPOSAL.md", {"phase": "research_refine", "iterations": result.iterations,
                                        "required_artifacts": ["refine-logs/FINAL_PROPOSAL.md"]})

    def _run_experiment_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the approved final proposal into an executable experiment plan."""
        self._validate_required_artifacts(["refine-logs/FINAL_PROPOSAL.md"])
        proposal_bytes = self._require_file("refine-logs/FINAL_PROPOSAL.md").read_bytes()
        if state.mode == "expert":
            approved = next((item for item in reversed(state.history)
                             if item.get("event") == "expert_approved" and item.get("node") == "refine"), {})
            if approved.get("sha256") != hashlib.sha256(proposal_bytes).hexdigest():
                raise ValueError("当前版本方案尚未批准，请从研究精炼阶段生成审阅摘要并确认")
            if approved.get("frozen_report"):
                from paper_agent.proposal_review import owned_path, sha
                frozen = owned_path(self._project, approved["frozen_report"]).read_bytes()
                if sha(frozen) != approved["sha256"]:
                    raise ValueError("已批准方案的冻结副本校验失败")
                proposal_bytes = frozen
        result = self._go(
            f"""{prompt}

已批准方案是方法的唯一来源。制定可直接实施的实验计划，自行组织章节；tracker 由运行器生成。
不要编写 tracker、改写方案或编造结果。

已批准方案路径：refine-logs/FINAL_PROPOSAL.md
""",
            phase="experiment_plan",
        )
        if state.mode == "expert":
            if self._require_file("refine-logs/FINAL_PROPOSAL.md").read_bytes() != proposal_bytes:
                raise ValueError("实验规划期间改写了已批准方案，计划未通过校验")
            if approved.get("frozen_report") and owned_path(self._project, approved["frozen_report"]).read_bytes() != proposal_bytes:
                raise ValueError("实验规划期间改写了冻结方案，计划未通过校验")
        self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        write_experiment_tracker(self._project, read_plan_blocks(self._project))
        return NodeResult("refine-logs/EXPERIMENT_PLAN.md", {"phase": "experiment_plan", "iterations": result.iterations,
                                        "source_phase": "experiment_plan",
                                        "required_artifacts": [
                                            "refine-logs/EXPERIMENT_PLAN.md",
                                            "refine-logs/EXPERIMENT_TRACKER.md",
                                        ]})

    def _run_experiment_bridge(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Own implementation, deployment, collection and plan reconciliation."""
        self._validate_required_artifacts([
            "refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
        ])
        if not (self._project / 'refine-logs/EXPERIMENT_TRACKER.md').exists():
            write_experiment_tracker(self._project, read_plan_blocks(self._project))
        completed_jobs = 0

        def execute():
            nonlocal completed_jobs
            try:
                manifest = self._experiment_manifest(state)
                self.experiments.validate_inputs(manifest)
                attempts = run_queue(self.experiments, state, manifest,
                                     on_progress=self.on_node_progress)
                completed_jobs = len(attempts)
                return ToolResult(json.dumps({
                    'status': 'completed', 'completed_jobs': len(attempts),
                    'next': '读取实际结果并撰写 EXPERIMENT_RESULTS.md。',
                }, ensure_ascii=False))
            except (AwaitingResults, ValueError, RuntimeError, OSError) as exc:
                return ToolResult(str(exc) + '；读取日志，修复后可以再次调用 run_experiments。'
                                  '无法完成的任务应如实说明其影响和限制。', True)

        def run_experiments(ctx: ToolContext) -> ToolResult:
            """通过支持续跑的队列执行 manifest 任务，每块 GPU 同时只运行一个训练任务。工具等待执行完成；模型 HTTP 超时不限制实验任务时长。复用有效的已完成任务。"""
            return execute()

        self.agent.add_tool(ToolSpec(run_experiments.__name__, run_experiments.__doc__, run_experiments),
                            phases=("experiment_bridge",))

        def complete():
            nonlocal completed_jobs
            if (self._project / 'experiments/manifest.json').is_file():
                statuses = self._refresh_experiment_tracker(state, self._experiment_manifest(state))
                completed_jobs = sum(item['status'] == 'completed' for item in statuses.values())
            self._validate_required_artifacts(['refine-logs/EXPERIMENT_RESULTS.md'])

        result = self._go(
            f"{prompt}\n负责实验实现、调用 run_experiments、分析结果并核对原始计划。"
            "通过 skill(name='run-experiment') 读取执行子技能，不通过 bash 启动训练。"
            "复用已完成任务；缺少后处理时补充后处理，不重复训练。"
            "在 refine-logs/EXPERIMENT_RESULTS.md 说明真实结果、失败项、未完成项及其研究影响，"
            "将可用结果交给 paper_plan；不能把失败或未执行的实验写成成功。tracker 由运行器生成。\n"
            + EXPERIMENT_INTERFACE,
            phase='experiment_bridge', completion_check=complete)
        return NodeResult('refine-logs/EXPERIMENT_RESULTS.md', {
            'phase': 'experiment_bridge', 'iterations': result.iterations,
            'manifest': 'experiments/manifest.json', 'completed_jobs': completed_jobs,
        })


    def _refresh_experiment_tracker(self, state, manifest):
        # Reconcile against current jobs and valid artifacts, including durable receipts
        # from an earlier workflow process. Changed prerequisites invalidate dependents.
        receipts = []
        for path in sorted((self._project / 'experiment-runs').glob('*/status.json')):
            receipts.append(json.loads(path.read_text(encoding='utf-8')))
        statuses = {}
        for job in manifest['jobs']:
            # Compare the execution specification even for failed/running attempts.
            matching = [r for r in receipts if receipt_matches_job({**r, 'status': 'completed'}, job)]
            latest = max(matching, key=lambda r: r.get('finished', r.get('started', '')), default=None)
            item = dict(id=job['id'], status='pending', execution_status='pending')
            if latest is not None:
                execution = latest['status']
                if latest.get('exit_code') == 0:
                    execution = 'completed'
                item.update(status=latest['status'], execution_status=execution,
                            error=latest.get('error'))
                if execution == 'completed':
                    issues = self.experiments.output_issues(job)
                    item.update(status='artifact_invalid' if issues else 'completed',
                                error='; '.join(f"{i['path']}: {i['problem']}" for i in issues))
            statuses[job['id']] = item
        dependencies = job_dependencies(manifest['jobs'])
        while True:
            invalid = {identifier for identifier, item in statuses.items()
                       if item['status'] == 'completed'
                       and any(statuses[dep]['status'] != 'completed' for dep in dependencies[identifier])}
            if not invalid:
                break
            for identifier in invalid:
                statuses[identifier].update(status='dependency_invalid')
        for identifier, item in statuses.items():
            if item['status'] in {'pending', 'dependency_invalid'}:
                invalid_deps = [dep for dep in dependencies[identifier] if statuses[dep]['status'] != 'completed']
                if invalid_deps:
                    item.update(status='dependency_invalid', error='依赖未通过校验：' + ', '.join(invalid_deps))
        write_experiment_tracker(self._project, read_plan_blocks(self._project), manifest['jobs'], statuses.values())
        return statuses

    @artifact_contract
    def _experiment_manifest(self, state=None) -> dict[str, Any]:
        self._require_file("experiments/manifest.json")
        manifest = self.experiments.load_manifest()
        return manifest


    @property
    def _project(self) -> Path:
        return Path(self.agent.project).resolve()

    def _require_file(self, relative: str) -> Path:
        path = self._project / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactValidationError(f"required artifact is missing or empty: {relative}")
        return path

    def _validate_required_artifacts(self, relatives: Iterable[str]) -> None:
        for relative in relatives:
            path = self._require_file(relative)
            if not path.read_text(encoding="utf-8").strip():
                raise ArtifactValidationError(f"required artifact is empty: {relative}")

    def _artifact_refs(self, node: str) -> list[dict[str, Any]]:
        """State holds evidence addresses; report content lives only in files."""
        refs = []
        for relative in self._review_files(node):
            path = self._project / relative
            if path.is_file():
                refs.append({"path": relative, "bytes": path.stat().st_size})
        return refs

    def _available_experiment_outputs(self, state) -> list[Path]:
        manifest = self._experiment_manifest(state)
        statuses = self._refresh_experiment_tracker(state, manifest)
        return [path for job in manifest['jobs']
                if statuses[job['id']]['status'] == 'completed'
                for output in job['expected_outputs']
                if (path := self.experiments.output_path(output['path'])).is_file()]

    def _run_paper_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        results = self._available_experiment_outputs(state)
        self._require_file("refine-logs/FINAL_PROPOSAL.md")
        self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        paths = [str(path.relative_to(self._project)) for path in results]
        result = self._go(
            f"{prompt}\n\n制定论文计划。可用的实测结果文件：{paths}。"
            "读取 EXPERIMENT_RESULTS.md，明确未完成实验和研究限制，不使用未验证结果支持结论。"
             , phase="paper_plan")
        self._require_file("PAPER_PLAN.md")
        return NodeResult("PAPER_PLAN.md", {"phase": "paper_plan", "results": paths, "iterations": result.iterations})

    def _run_paper_figure(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        result = self._go(
            f"{prompt}\n依据 PAPER_PLAN.md 和真实实验结果制作图表。"
            "自行选择代码绘图或 generate_image，使用 view_image 检查最终图片。"
            "将 LaTeX 引用写入 figures/latex_includes.tex，并在 figures/figure_plan.md 说明图的用途和证据。"
            "额外的视觉审查 JSON、逐文件哈希和固定回退清单均不作为完成条件。",
            phase="paper_figure")
        return NodeResult("figures/latex_includes.tex", {"phase": "paper_figure", "iterations": result.iterations})

    def _run_paper_write(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        self._require_file("figures/latex_includes.tex")
        result = self._go(
            f"{prompt}\n根据 PAPER_PLAN.md 和已验证的 results/ 撰写论文。仅使用已生成的图和已核实的引用。"
            "主入口写入 paper/main.tex，自行组织章节与参考文献文件。尽可能保留已有论文内容。", phase="paper_write")
        self._require_file("paper/main.tex")
        return NodeResult("paper/main.tex", {"phase": "paper_write", "iterations": result.iterations})

    def _compile_pdf(self) -> Path:
        self._require_file("paper/main.tex")
        return compile_pdf(self._project)

    def _run_paper_compile(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("paper/main.tex")
        if (state is not None and state.mode == "expert" and state.expert_checkpoint
                and state.expert_checkpoint.get("node") == "paper_compile"
                and state.expert_checkpoint.get("status") == "revision_requested"):
            self._go(f"{prompt}\n根据用户意见修订 paper/ 中的源码，保持可编译。",
                     phase="paper_compile", completion_check=lambda: self._compile_pdf())
        try:
            pdf = self._compile_pdf()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            def check():
                try:
                    self._compile_pdf()
                except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                    raise ArtifactValidationError(str(error)) from error

            result = self._go(
                f"{prompt}\n修复 paper/ 中的 LaTeX 编译。读取编译日志，修复源码或安装缺失依赖，并验证编译。"
                f"错误：\n{exc}",
                phase="paper_compile", completion_check=check)
            pdf = self._require_file("paper/main.pdf")
            return NodeResult(str(pdf.relative_to(self._project)),
                              {"phase": "paper_compile", "iterations": result.iterations})
        return NodeResult(str(pdf.relative_to(self._project)), {"phase": "paper_compile"})

    def _run_improvement(self, state: WorkflowState, prompt: str) -> NodeResult:
        from paper_agent.integrations.review import ReviewRequest, ReviewRunner

        paper = self._require_file("paper/main.tex").parent
        self._require_file("paper/main.pdf")
        log = paper / "PAPER_IMPROVEMENT_LOG.md"
        from paper_agent.integrations.review import paper_review_inputs

        checkpoint = state.improvement_checkpoint if self.config.improvement_resume else {}
        start_round = checkpoint.get("round", self.config.improvement_start_round)
        stage = checkpoint.get("stage", "review")
        if start_round not in (1, 2) or stage not in {"review", "revision", "compile", "completed"}:
            raise ValueError("Invalid improvement checkpoint")
        if start_round == 1 and not checkpoint and not self.config.improvement_resume:
            shutil.copy2(paper / "main.pdf", paper / "main_round0_original.pdf")
            log.write_text("# 论文改进日志\n", encoding="utf-8")
        else:
            self._require_file("paper/main_round0_original.pdf")
            self._require_file("paper/PAPER_IMPROVEMENT_LOG.md")
            if start_round == 2:
                for relative in ("paper/main_round1.pdf", "paper/review_round1.md"):
                    self._require_file(relative)

        def progress(round_number, stage, **details):
            state.improvement_checkpoint = {"round": round_number, "stage": stage, **details}
            if self.on_node_progress:
                self.on_node_progress("improvement", state, state.improvement_checkpoint)

        rounds = []
        for round_number in range(start_round, 3):
            report = Path(f"paper/review_round{round_number}.md")
            if stage == "completed":
                break
            if stage not in {"revision", "compile"}:
                inputs = set(paper_review_inputs(self._project))
                timeout = (self.config.paper_review_timeout if round_number == 1
                           else self.config.paper_review_round2_timeout)
                instructions = (
                    "完整独立审稿：仅根据当前论文源码、PDF 和真实研究证据评估论文。"
                    "不读取旧审稿报告、改进日志或作者修改说明，不根据历史评分或预期进步调整判断。"
                    "给出十分制评分、优点、按 CRITICAL/MAJOR/MINOR 分级的问题、"
                    "修改建议、视觉审查及投稿就绪判断。检查科学逻辑、证据、引用、图与图注及排版。"
                )
                progress(round_number, "review", timeout_seconds=timeout,
                         stage_started_at=datetime.now(timezone.utc).isoformat())
                review = ReviewRunner(self._project, timeout=timeout).run(ReviewRequest(
                    kind="paper", instructions=(f"用户研究任务：{prompt}\n" + instructions +
                        "先读实验结果摘要，只有发现具体矛盾才检查对应 results/ 证据，不全面重复审计。"
                        "只检查清单中的当前稿件，排除 audit/、旧稿、备份和未引用图片。"
                        "保留不利结果和局限，不夸大或无故弱化结论，不编造引用，不启动实验或训练。"),
                    inputs=tuple(sorted(inputs)), output=report, save_diagnostics=True))
                if review.status != "completed":
                    progress(round_number, "review", status=review.status,
                             elapsed_seconds=review.elapsed_seconds, error=review.error)
                    raise RuntimeError(f"Paper review round {round_number} did not complete: {review.error}")
                progress(round_number, "revision")
            self._require_file(str(report))
            heading = f"## 第 {round_number} 轮原始审稿"
            if heading not in log.read_text(encoding="utf-8"):
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"\n{heading}\n\n" + (self._project / report).read_text(encoding="utf-8") + "\n")
            iterations = 0
            if stage != "compile":
                result = self._go(
                    f"{prompt}\n实施第 {round_number}/2 轮论文改进。读取 {report}，按严重程度修复当前论文，"
                    "运行器已完成本轮独立评审；本次只修订，不重复发起评审。"
                    "两轮评审均由运行器根据当前稿独立执行。只使用已有实验和可核实引用，"
                    "不改动实验结果，不启动新实验。将实际修改和未解决问题追加到 "
                    "paper/PAPER_IMPROVEMENT_LOG.md。本轮完成后由运行器重新编译。",
                    phase="improvement")
                iterations = result.iterations
                progress(round_number, "compile")
            compiled = self._run_paper_compile(state, prompt)
            shutil.copy2(self._project / compiled.artifact, paper / f"main_round{round_number}.pdf")
            rounds.append({"round": round_number, "review": str(report),
                           "iterations": iterations})
            progress(round_number + 1 if round_number == 1 else 2,
                     "review" if round_number == 1 else "completed")
            stage = "review"
        return NodeResult("paper/main.pdf", {"phase": "improvement", "rounds": rounds,
                                               "log": "paper/PAPER_IMPROVEMENT_LOG.md"})

    def _next_node(self, node: str) -> str | None:
        return NEXT_NODE.get(node)

    @staticmethod
    def _fail(state: WorkflowState, message: str) -> WorkflowState:
        state.status = "failed"
        state.error = message
        return state


__all__ = ["WorkflowConfig", "WorkflowRunner", "WorkflowState"]
