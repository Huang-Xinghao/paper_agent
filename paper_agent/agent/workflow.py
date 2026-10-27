"""Deterministic workflow controller for research and paper production."""

from __future__ import annotations

import json
import hashlib
import re
import csv
import shutil
import subprocess
import os
import math
from functools import wraps
from .experiment_queue import run_queue
from .experiment_executor import ExperimentExecutor, AwaitingResults, ExperimentExecutionError, receipt_matches_job, job_dependencies
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .loop import ArtifactValidationError, PhaseAgent
from paper_agent.tools import ToolContext, ToolResult, ToolSpec
from .experiment_handoff import EXPERIMENT_INTERFACE, read_plan_blocks, write_experiment_tracker
from paper_agent.tools.battery_resources import search_battery_resources, read_battery_resource
from paper_agent.tools.data_access import registered_data_sources
from paper_agent.tools.literature import search_papers, discover_local_papers, read_local_paper
from paper_agent.tools.wiki import search_wiki, read_wiki, resolve_wiki_root

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
    task_prompt: str = ""
    node_instructions: str = ""
    mode: Literal["auto", "expert"] = "auto"
    expert_checkpoint: dict[str, Any] | None = None
    current_node: str = "ideas"
    status: WorkflowStatus = "running"
    paper_status: PaperStatus = "pending"
    artifacts: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    transitions: int = 0  # Lifetime successful node executions; never a run budget.
    fault_retry_count: int = 0
    failed_node: str | None = None
    error: str | None = None
    node_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    experiment_repair_count: int = 0
    experiment_feedback: dict[str, Any] | None = None
    experiment_completed_receipts: list[dict[str, Any]] = field(default_factory=list)
    improvement_checkpoint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        return cls(**value)


@dataclass(frozen=True)
class WorkflowConfig:
    """Routing and retry limits for the research-to-paper workflow."""

    start_node: str = "literature"
    max_node_steps_per_run: int | None = None
    max_experiment_repairs: int = 3
    improvement_start_round: int = 1
    paper_review_timeout: float = 1800
    paper_review_round2_timeout: float = 1200
    improvement_resume: bool = False
    mode: Literal["auto", "expert"] = "auto"


@dataclass(frozen=True)
class IdeaNodeConfig:
    max_total_gpu_hours: float = 8.0
    search_paper_number: int = 15


@dataclass
class NodeResult:
    """Output returned by a workflow node handler."""

    artifact: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowRunner:
    """Run the research-to-paper workflow around one :class:`PhaseAgent`.

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
        if type(self.idea_config.search_paper_number) is not int or not 1 <= self.idea_config.search_paper_number <= 50:
            raise ValueError("search_paper_number must be between 1 and 50")
        if self.config.max_node_steps_per_run is not None and self.config.max_node_steps_per_run < 1:
            raise ValueError("max_node_steps_per_run must be positive")
        if self.config.improvement_start_round not in (1, 2):
            raise ValueError("improvement_start_round must be 1 or 2")
        for name in ("paper_review_timeout", "paper_review_round2_timeout"):
            if not math.isfinite(getattr(self.config, name)) or getattr(self.config, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("max_experiment_repairs",):
            value = getattr(self.config, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        self.run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
        self.on_node_start = on_node_start
        self.on_node_end = on_node_end
        self.on_node_progress = on_node_progress
        self.require_image_figure = os.environ.get(
            "PAPER_AGENT_ENABLE_IMAGE_FIGURE", "").lower() in {"1", "true", "yes"}
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
            stop_after: str | None = None) -> WorkflowState:
        """Run until a terminal state and return the complete workflow state."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        state = state or WorkflowState(self.run_id, current_node=self.config.start_node,
                                       task_prompt=prompt, mode=self.config.mode)
        if not state.task_prompt:
            state.task_prompt = prompt
        state.node_instructions = prompt if prompt != state.task_prompt else ""
        instruction_node = state.current_node
        if state.status == "awaiting_results":
            state.status = "running"
            state.error = None
        if state.status != "running":
            return state

        # Guard routing loops within this invocation; prior runs cost no budget.
        steps_this_run = 0
        # Experiment repair/expansion now happens inside the bridge tool loop.
        step_limit = self.config.max_node_steps_per_run or (
            len(self._nodes) + 6 + 4 * self.config.max_experiment_repairs)
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
                node_prompt = state.task_prompt
                instructions = state.node_instructions if node == instruction_node else ""
                if instructions:
                    node_prompt += f"\n\n本次节点补充指令（仅适用于 {instruction_node}）：\n{instructions}"
                advance_revision = bool(
                    state.expert_checkpoint
                    and state.expert_checkpoint.get("node") == node
                    and state.expert_checkpoint.get("status") == "revision_requested"
                    and state.expert_checkpoint.get("advance_after_revision")
                )
                review_required = state.mode == "expert" and node != "refine" and not advance_revision
                self._expert_node = node if review_required else None
                if self._expert_node:
                    (self._project / self._checkpoint_path(node)).unlink(missing_ok=True)
                result = handler(state, node_prompt)
                if review_required:
                    if not (self._project / self._checkpoint_path(node)).is_file():
                        self._go("节点执行已完成。仅审阅当前产物并生成专家检查点，不重复执行节点任务。",
                                 phase=result.metadata.get("phase", node),
                                 completion_check=lambda: None)
                    checkpoint = self._read_checkpoint(node)
                    report = self._require_file(self._review_files(node)[0])
                    state.expert_checkpoint = {
                        **checkpoint, "node": node, "next_node": self._next_node(state, node),
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
            self._expert_node = None
            state.artifacts[node] = result.artifact
            state.node_results[node] = {
                "artifact": result.artifact,
                **result.metadata,
                "node_instructions": instructions,
            }
            messages = getattr(self.agent, "last_state", None)
            if messages is not None:
                state.node_results[node]["tool_calls"] = [
                    message.get("name") for message in getattr(messages, "messages", [])
                    if message.get("role") == "tool"
                ]
            state.history.append({
                "node": node,
                "metadata": result.metadata,
                "node_instructions": instructions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            state.transitions += 1
            if node in {"paper_compile", "improvement"}:
                state.paper_status = "pdf_ready"
            if advance_revision:
                # Reuse approval validation and routing, without asking the user
                # to approve the same node again after applying their feedback.
                try:
                    state.expert_checkpoint.update(status="pending", next_node=self._next_node(state, node))
                    state.status = "awaiting_user"
                    self.resume_checkpoint(state)
                except Exception as exc:
                    state.failed_node = node
                    return self._fail(state, f"node {node} revision validation failed: {exc}")
                if self.on_node_end:
                    self.on_node_end(node, state, result)
                continue
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
                next_node = self._next_node(state, node)
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
                          advance_after_revision: bool = False) -> WorkflowState:
        """Approve current files, or prepare to revise the paused node."""
        checkpoint = state.expert_checkpoint
        if (state.status != "awaiting_user" or state.current_node not in self._nodes
                or not checkpoint or checkpoint.get("status") != "pending"
                or checkpoint.get("node") != state.current_node):
            raise ValueError("没有待确认的专家检查点")
        if not revise:
            node = state.current_node
            for relative in self._review_files(node):
                path = self._require_file(relative)
                if path.suffix != ".pdf" and not path.read_text(encoding="utf-8").strip():
                    raise ValueError(f"节点产物不能为空：{relative}")
            if node == "experiment_bridge":
                statuses = self._refresh_experiment_tracker(state, self._experiment_manifest())
                invalid = [identifier for identifier, item in statuses.items()
                           if item["status"] != "completed"]
                if invalid:
                    raise ValueError("实验任务或产物未完成校验，请修复或输入意见重新执行：" + ", ".join(invalid))
            if node == "experiment_plan":
                write_experiment_tracker(self._project, read_plan_blocks(self._project))
            if node in {"paper_compile", "improvement"}:
                self._compile_pdf()
            report = self._require_file(checkpoint["report"])
            content = report.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("节点产物不能为空，请编辑后再确认")
            if node in {"literature", "ideas"}:
                state.artifacts[node] = content
                state.node_results[node]["artifact"] = content
            checkpoint["confirmed_files"] = {
                relative: hashlib.sha256(self._require_file(relative).read_bytes()).hexdigest()
                for relative in self._review_files(node)
            }
            checkpoint["confirmed_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
            checkpoint["status"] = "approved"
            state.history.append({
                "event": "expert_approved", "node": node,
                "report": checkpoint["report"], "sha256": checkpoint["confirmed_sha256"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            next_node = checkpoint["next_node"]
            if checkpoint.get("stop_after") == node or next_node is None:
                state.status = "completed"
            else:
                state.current_node = next_node
        else:
            checkpoint["status"] = "revision_requested"
            checkpoint["advance_after_revision"] = advance_after_revision
        if state.status != "completed":
            state.status = "running"
        state.error = None
        state.node_instructions = ""
        return state


    def _go(self, prompt: str, *, phase: str,
            completion_check: Callable[[], None] | None = None):
        check = completion_check or (lambda: self._validate_phase_outputs(phase))
        node = getattr(self, "_expert_node", None)
        if node:
            checkpoint_path = self._checkpoint_path(node)
            (self._project / checkpoint_path).unlink(missing_ok=True)
            question_focus = {
                "literature": (
                    "本节点问题围绕报告实际提出的研究 gap：点明具体 gap、最接近的已有工作及尚未解决的差异，"
                    "请用户判断哪个 gap 更值得优先研究、是否符合其领域经验、需要收窄或调整哪些边界。"
                    "总结应突出 gap 及其文献证据强弱。若存在多个 gap，应明确比较其研究价值与区别，"
                    "让用户聚焦后续构思的研究问题。不要把提问重心放在输入循环数、预测输出格式、"
                    "模型结构、训练策略等实现细节，也不要将尚未验证的 gap 当作已确认的领域空白。"
                ),
                "ideas": (
                    "本节点问题围绕 IDEA_REPORT.md 实际提出的 idea：点明方案名称、核心机制及其对应的 gap，"
                    "请用户选择或调整优先推进的 idea、核心创新主张、关键假设与方案取舍。"
                    "总结应突出提出了什么 idea、为什么可能解决该 gap，以及区别于近邻工作的机制。"
                    "若有多个候选，应具体比较；若只有一个，应围绕该方案的价值、假设与可证伪性提问。"
                    "不要重新泛泛询问研究方向，不要以训练超参数等实现细节代替对 idea 本身的讨论。"
                ),
                "experiment_plan": (
                    "本节点问题围绕实际生成的 EXPERIMENT_PLAN.md，请用户针对具体实验区块提出修改："
                    "哪些实验需要增加、删除或调整优先级，基线、消融、数据划分、评价指标及计算预算是否需要修改。"
                    "点明计划中的具体设置与取舍，让用户能按区块编号回答，不要泛泛询问是否同意实验计划。"
                ),
                "paper_plan": (
                    "本节点必须请用户逐张确认 PAPER_PLAN.md 中计划的每张图的图注。"
                    "先在计划正文中为所有主文及补充图列出编号和完整图注草稿，"
                    "明确各子图含义、实验条件、指标及必要的证据限制，不得只列图标题。"
                    "在检查点问题中按图编号逐一展示完整图注草稿，请用户逐张确认或给出修改意见，"
                    "不得遗漏任何计划图，也不得仅笼统询问图注是否合适。"
                    "可将全部逐图确认项合并为一个问题，其余问题讨论论文计划的关键取舍，问题总数仍为 2–3 个。"
                ),
            }.get(node, "")
            prompt += (
                f"\n\n当前为专家模式，完成 {node} 后供用户审阅。先读取已有产物，保留用户修改。"
                f"本节点主要产物：{json.dumps(self._review_files(node), ensure_ascii=False)}。"
                f"完成后根据实际产物写入 {checkpoint_path}，格式为"
                '{"summary":"本次结果的简短中文总结", "questions":["问题1", "问题2"]}。'
                "提出 2–3 个基于本节点实际结果、关键取舍或证据缺口的问题，避免泛泛询问是否满意。"
                "不要自行确认或执行下游节点。"
                + question_focus
            )
            original_check = check

            def check():
                original_check()
                self._read_checkpoint(node)
        return self.agent.go(prompt, phase=phase, completion_check=check)

    @staticmethod
    def _review_files(node: str) -> list[str]:
        return {
            "literature": ["literature/RESEARCH_REPORT.md"],
            "data_search": ["data-search/DATA_REPORT.md"],
            "ideas": ["idea-stage/IDEA_REPORT.md"],
            "refine": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_plan": ["refine-logs/EXPERIMENT_PLAN.md"],
            "experiment_bridge": ["refine-logs/EXPERIMENT_RESULTS.md", "experiments/manifest.json"],
            "paper_plan": ["PAPER_PLAN.md"],
            "paper_figure": ["figures/figure_plan.md", "figures/latex_includes.tex"],
            "paper_write": ["paper/main.tex", "paper/references.bib"],
            "paper_compile": ["paper/main.tex", "paper/main.pdf"],
            "improvement": ["paper/PAPER_IMPROVEMENT_LOG.md", "paper/main.tex", "paper/main.pdf"],
        }[node]

    @classmethod
    def _checkpoint_path(cls, node: str) -> str:
        if node == "literature":
            return "literature/EXPERT_CHECKPOINT.json"
        return str(Path(cls._review_files(node)[0]).parent / f"{node}_EXPERT_CHECKPOINT.json")

    def _validate_phase_outputs(self, phase: str) -> None:
        required = {
            "literature": ["literature/RESEARCH_REPORT.md"],
            "data_search": ["data-search/DATA_REPORT.md"],
            "ideas": ["idea-stage/IDEA_REPORT.md"],
            "research_refine": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_plan": ["refine-logs/EXPERIMENT_PLAN.md"],
            "paper_plan": ["PAPER_PLAN.md"],
            "paper_figure": ["figures/latex_includes.tex", "figures/figure_plan.md"],
            "paper_write": ["paper/main.tex", "paper/references.bib"],
        }
        self._validate_required_artifacts(required.get(phase, []))
        if phase == "experiment_plan":
            try:
                write_experiment_tracker(self._project, read_plan_blocks(self._project))
            except ValueError as exc:
                raise ArtifactValidationError(str(exc)) from exc
        if phase == "experiment_bridge":
            self._experiment_manifest()
        elif phase == "paper_figure":
            messages = getattr(self.agent.last_state, "messages", [])
            self._figure_receipts(messages)
            self._figure_visual_checks(messages)
        elif phase == "paper_write":
            sections = (self._project / "paper/sections").glob("*.tex")
            if not any(path.is_file() and path.read_text(encoding="utf-8").strip() for path in sections):
                raise ArtifactValidationError("paper/sections/ requires a nonempty TeX section")

    def _experiment_evidence_snapshot(self):
        paths = set()
        for pattern in ("results/**/*", "experiment-runs/**/*",
                        "project-config.json"):
            paths.update(p for p in self._project.glob(pattern) if p.is_file())
        return {str(p.relative_to(self._project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths}

    def start_from_project(self, node: str, prompt: str,
                           state: WorkflowState | None = None) -> WorkflowState:
        """Prepare an explicit entry point from durable outputs, checking its inputs."""
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        if state is None:
            state = WorkflowState(self.run_id, task_prompt=prompt, mode=self.config.mode)
        required = {
            "data_search": ["literature/RESEARCH_REPORT.md"],
            "ideas": ["literature/RESEARCH_REPORT.md", "data-search/DATA_REPORT.md"],
            "refine": ["idea-stage/IDEA_REPORT.md"],
            "experiment_plan": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_bridge": ["refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md"],
            "paper_plan": ["refine-logs/FINAL_PROPOSAL.md",
                           "refine-logs/EXPERIMENT_PLAN.md"],
            "paper_figure": ["PAPER_PLAN.md"],
            "paper_write": ["PAPER_PLAN.md", "figures/latex_includes.tex"],
            "paper_compile": ["paper/main.tex"],
            "improvement": ["paper/main.tex", "paper/main.pdf"],
        }
        for relative in required.get(node, []):
            self._require_file(relative)
        if node == "refine":
            state.artifacts["ideas"] = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if not state.task_prompt:
            state.task_prompt = prompt
        state.node_instructions = prompt if prompt != state.task_prompt else ""
        state.current_node = node
        state.expert_checkpoint = None
        state.status = "running"
        state.error = None
        return state

    def _research_context(self, query: str, *, stage: str, max_results: int = 5) -> dict[str, Any]:
        """Read bounded source material for literature or research refinement."""
        project = self._project
        context: dict[str, Any] = {"wiki": {"status": "unavailable"}}
        tool_ctx = ToolContext(project)
        wiki_root = resolve_wiki_root()
        context["wiki"]["root"] = str(wiki_root)
        if wiki_root.is_dir():
            pages = []
            seen = set()
            errors = []
            for name in ("index.md", "query_pack.md"):
                if (wiki_root / name).is_file():
                    seen.add(name)
                    pages.append(name)
            try:
                result = search_wiki(tool_ctx, query=query[:500], limit=5)
                if result.is_error:
                    raise ValueError(result.output)
                for match in json.loads(result.output)["matches"]:
                    path = Path(match["path"])
                    if not path.is_absolute():
                        path = project / path
                    relative = path.resolve().relative_to(wiki_root).as_posix()
                    if relative not in seen and path.parent.name in {"papers", "ideas", "experiments", "claims"}:
                        seen.add(relative)
                        pages.append(relative)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"search: {exc}")
            records = []
            for relative in pages[:7]:
                try:
                    result = read_wiki(tool_ctx, path=str(wiki_root / relative), limit=220)
                    if result.is_error:
                        raise ValueError(result.output)
                    payload = json.loads(result.output)
                    records.append({"path": payload["path"], "content": payload["content"][:6000],
                                    "truncated": payload["next_offset"] is not None or len(payload["content"]) > 6000})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(f"{relative}: {exc}")
            context["wiki"] = {"status": "partial" if errors and records else
                               "read" if records else "read_failed" if errors else "empty",
                               "pages": records, "errors": errors, "root": str(wiki_root)}

        if stage == "literature":
            try:
                result = search_papers(tool_ctx, query=query[:500], max_results=max_results)
                if result.is_error:
                    raise ValueError(result.output)
                payload = json.loads(result.output)
                context["online_papers"] = {"status": "searched", "query": payload["query"],
                                            "papers": payload["papers"]}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                context["online_papers"] = {"status": "search_failed", "error": str(exc)}
        elif stage == "research_refine":
            knowledge_root = self.agent.knowledge_root
            if not knowledge_root or not knowledge_root.is_dir():
                local_root = Path(__file__).resolve().parents[2] / "电池资源库"
                knowledge_root = project / "电池资源库" if (project / "电池资源库").is_dir() else local_root
            if not knowledge_root.is_dir():
                context["battery_resources"] = {"status": "unavailable"}
            else:
                battery_ctx = ToolContext(project, knowledge_roots=(knowledge_root,))
                resources = []
                errors = []
                seen_resources = set()
                for resource_type in ("dataset", "input", "method"):
                    try:
                        result = search_battery_resources(battery_ctx, query=query[:500],
                                                          stage="experiment plan",
                                                          resource_types=[resource_type], top_k=2)
                        if result.is_error:
                            raise ValueError(result.output)
                        for item in json.loads(result.output)["resources"]:
                            resource_id = item["resource_id"]
                            if resource_id in seen_resources:
                                continue
                            read_result = read_battery_resource(battery_ctx, resource_id, max_chars=4000)
                            if read_result.is_error:
                                raise ValueError(read_result.output)
                            payload = json.loads(read_result.output)
                            resources.append({"resource_id": payload["resource_id"],
                                              "path": payload["absolute_path"], "type": payload["type"],
                                              "content": payload["content"][:4000],
                                              "truncated": payload["truncated"]})
                            seen_resources.add(resource_id)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        errors.append(f"{resource_type}: {exc}")
                context["battery_resources"] = {"status": "partial" if errors and resources else
                                                "read" if resources else "read_failed" if errors else "empty",
                                                "resources": resources, "errors": errors}
        if stage == "literature":
            context.update(self._literature_inputs(tool_ctx, query))
        return context

    def _literature_inputs(self, ctx: ToolContext, query: str) -> dict[str, Any]:
        """Screen existing local PDFs before asking the model to synthesize."""
        def payload(result):
            if result.is_error:
                raise ValueError(result.output)
            return json.loads(result.output)

        def screen(path, pages):
            record = payload(read_local_paper(ctx, path=path, max_pages=pages))
            text = record.get("text", "")
            if not text.strip():
                raise ValueError("No extractable PDF text; screening requires OCR or another source")
            record["text"] = text[:6000]
            record["truncated"] = len(text) > 6000
            return record

        records, errors = [], []
        found = 0
        try:
            discovery = payload(discover_local_papers(ctx, query=query[:500], max_papers=20))
            found = discovery["found"]
            for paper in discovery["papers"]:
                try:
                    records.append(screen(paper["path"], 3))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(f"{paper['path']}: {exc}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
        local = {"status": "partial" if errors and records else "read" if records else
                 "read_failed" if errors else "empty", "found": found,
                 "papers": records, "errors": errors}
        return {"local_papers": local}

    def _run_data_search(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Select and understand datasets before proposing research ideas."""
        literature_path = "literature/RESEARCH_REPORT.md"
        self._validate_required_artifacts([literature_path])
        knowledge_root = getattr(self.agent, "knowledge_root", None)
        sources = registered_data_sources((knowledge_root,) if knowledge_root else ())
        result = self._go(
            f"原始任务：\n{prompt}\n\n"
            "将用于生成研究想法的数据报告写入 data-search/DATA_REPORT.md。\n\n"
            f"已登记的数据源：\n{json.dumps(sources, ensure_ascii=False)}",
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
                   max_total_gpu_hours: float | None = None,
                   search_paper_number: int | None = None) -> NodeResult:
        max_total_gpu_hours = (self.idea_config.max_total_gpu_hours if max_total_gpu_hours is None
                               else max_total_gpu_hours)
        search_paper_number = (self.idea_config.search_paper_number if search_paper_number is None
                               else search_paper_number)
        if not math.isfinite(max_total_gpu_hours) or max_total_gpu_hours <= 0:
            raise ValueError("max_total_gpu_hours must be a finite positive number")
        if type(search_paper_number) is not int or not 1 <= search_paper_number <= 50:
            raise ValueError("search_paper_number must be between 1 and 50")
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
            f"原始任务：\n{prompt}\n\n当前节点任务：\n{instruction}\n"
            f"研究想法节点参数：\nMAX_TOTAL_GPU_HOURS={max_total_gpu_hours:g}; "
            f"SEARCH_PAPER_NUMBER={search_paper_number}. "
            "GPU 预算是所有拟议预实验的总估算值，不代表获准执行这些实验。论文数量参数作为检索目标；"
            "复用已有文献报告，并识别证据覆盖缺口。\n\n"
            "使用 search_papers 补充证据缺口。",
            phase="ideas",
        )
        idea = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if not idea.strip():
            raise ValueError("idea-stage/IDEA_REPORT.md is empty")
        return NodeResult(idea, {"phase": "ideas", "iterations": result.iterations,
                                        "canonical_path": "idea-stage/IDEA_REPORT.md",
                                        "literature_source": literature_path,
                                        "literature_sha256": hashlib.sha256(literature.encode("utf-8")).hexdigest(),
                                        "data_source": data_path,
                                        "data_sha256": hashlib.sha256(data_report.encode("utf-8")).hexdigest(),
                                        "idea_parameters": {
                                            "max_total_gpu_hours": max_total_gpu_hours,
                                            "search_paper_number": search_paper_number}})


    @staticmethod
    def _research_sources(research: dict[str, Any]) -> dict[str, Any]:
        return {
            "wiki": [page["path"] for page in research.get("wiki", {}).get("pages", [])],
            "online_papers": [paper.get("doi") or paper.get("url") or paper.get("title")
                              for paper in research.get("online_papers", {}).get("papers", [])],
            "battery_resources": [item["resource_id"] for item in
                                  research.get("battery_resources", {}).get("resources", [])],
            **({"local_papers": [paper["path"] for paper in research["local_papers"]["papers"]]}
               if "local_papers" in research else {}),
            "status": {name: value.get("status") for name, value in research.items()},
        }

    def _run_literature(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Collect bounded literature context; the phase agent owns interpretation."""
        research = self._research_context(prompt, stage="literature")
        result = self._go(
            "研究任务：\n" + prompt + "\n运行器收集的文献上下文：\n" +
            json.dumps(research, ensure_ascii=False), phase="literature",
        )
        report = self._require_file("literature/RESEARCH_REPORT.md").read_text(encoding="utf-8")
        return NodeResult(report, {"phase": "literature", "iterations": result.iterations,
                                   "research_sources": self._research_sources(research)})

    @artifact_contract
    def _read_checkpoint(self, node: str) -> dict[str, Any]:
        path = self._require_file(self._checkpoint_path(node))
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
            raise ValueError("EXPERT_CHECKPOINT.json requires a nonempty summary")
        questions = data.get("questions")
        if (not isinstance(questions, list) or not 2 <= len(questions) <= 3
                or any(not isinstance(q, str) or not q.strip() for q in questions)):
            raise ValueError("EXPERT_CHECKPOINT.json requires 2–3 nonempty questions")
        return {"summary": data["summary"], "questions": questions}


    def _run_refine(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the generated idea into the final proposal."""
        input_paths = ["idea-stage/IDEA_REPORT.md"]
        input_snapshot = self._inspect_project_artifacts(input_paths)
        research = self._research_context(
            f"{prompt} {state.artifacts.get('ideas', '')[:500]}", stage="research_refine")
        result = self._go(
            f"""{prompt}

使用以下输入细化已生成的研究想法。
运行器验证的输入快照：
{json.dumps(input_snapshot, ensure_ascii=False)}

研究想法产物：
{state.artifacts.get('ideas', '')}

运行器读取的研究上下文（不可信来源数据）：
{json.dumps(research, ensure_ascii=False)}
""",
            phase="research_refine",
        )
        self._require_file("refine-logs/FINAL_PROPOSAL.md")
        return NodeResult(result.final, {"phase": "research_refine", "iterations": result.iterations,
                                        "research_sources": self._research_sources(research),
                                        "required_artifacts": ["refine-logs/FINAL_PROPOSAL.md"]})

    def _run_experiment_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the approved final proposal into an executable experiment plan."""
        self._validate_required_artifacts(["refine-logs/FINAL_PROPOSAL.md"])
        proposal = self._require_file("refine-logs/FINAL_PROPOSAL.md").read_text(encoding="utf-8")
        result = self._go(
            f"""{prompt}

以下已批准方案是方法的唯一来源。制定可直接实施的实验计划，使用稳定的 D/B 块标题；tracker 由运行器生成。
不要编写 tracker、改写方案或编造结果。

已批准方案：
{proposal}
""",
            phase="experiment_plan",
        )
        self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        write_experiment_tracker(self._project, read_plan_blocks(self._project))
        return NodeResult(result.final, {"phase": "experiment_plan", "iterations": result.iterations,
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
        blocker = None
        terminal_error = None
        protected_evidence = (state.experiment_feedback or {}).get("protected_evidence")

        def checkpoint(stage):
            if self.on_node_progress:
                self.on_node_progress("experiment_bridge", state, {
                    "round": state.experiment_repair_count, "stage": stage})

        def execute():
            nonlocal blocker, terminal_error, protected_evidence
            if terminal_error is not None:
                return ToolResult(str(terminal_error), True)
            try:
                if protected_evidence is not None:
                    current_evidence = self._experiment_evidence_snapshot()
                    changed = sorted(path for path in current_evidence.keys() | protected_evidence.keys()
                                     if current_evidence.get(path) != protected_evidence.get(path))
                    if changed:
                        details = "; ".join(
                            f"{path} ({'added' if path not in protected_evidence else 'removed' if path not in current_evidence else 'modified'})"
                            for path in changed[:20])
                        if len(changed) > 20:
                            details += f"; ... ({len(changed)} files total)"
                        raise RuntimeError(
                            "Experiment repair modified protected evidence or resource settings: " + details)
                    protected_evidence = None
                    # Consume the guard durably before the runner writes new evidence.
                    # A later model/API failure may resume this same bridge invocation.
                    if state.experiment_feedback is not None:
                        state.experiment_feedback.pop("protected_evidence", None)
                manifest = self._experiment_manifest()
                self._refresh_experiment_tracker(state, manifest)
                self.experiments.validate_inputs(manifest)
                checkpoint("running_jobs")
                receipts = run_queue(self.experiments, state, manifest,
                                     on_progress=lambda _node, _state, detail:
                                     self.on_node_progress("experiment_bridge", state, detail)
                                     if self.on_node_progress else None)
                result = {"status": "completed", "completed_jobs": len(receipts),
                          "receipts": [{"id": r["job"]["id"], "status": r["status"],
                                        "log": r["log"]} for r in receipts],
                          "next": "读取实际结果，逐一核对原计划的所有 D/B 块，并撰写 EXPERIMENT_RESULTS.md。tracker 由运行器更新。"}
                blocker = None
                checkpoint("results_ready")
                return ToolResult(json.dumps(result, ensure_ascii=False))
            except AwaitingResults as exc:
                blocker = exc
                checkpoint("awaiting_results")
                return ToolResult(str(exc), True)
            except ExperimentExecutionError as exc:
                state.experiment_repair_count += 1
                protected_evidence = self._experiment_evidence_snapshot()
                state.experiment_feedback = {"source_node": "experiment_bridge", "error": str(exc),
                                             "attempt": state.experiment_repair_count,
                                             "protected_evidence": protected_evidence}
                if state.experiment_repair_count > self.config.max_experiment_repairs:
                    terminal_error = RuntimeError(f"Experiment repair limit reached: {exc}")
                checkpoint("repair_required")
                return ToolResult(str(terminal_error or exc) +
                                  "；读取已保存的日志，修复代码后再次调用 run_experiments。", True)
            except (ValueError, RuntimeError) as exc:
                if not isinstance(exc, (ValueError, ArtifactValidationError)):
                    terminal_error = exc
                return ToolResult(str(exc), True)

        def run_experiments(ctx: ToolContext) -> ToolResult:
            """通过支持续跑的队列执行 manifest 任务，每块 GPU 同时只运行一个训练任务。工具等待执行完成；模型 HTTP 超时不限制实验任务时长。复用有效的已完成任务。"""
            return execute()

        self.agent.add_tool(ToolSpec(run_experiments.__name__, run_experiments.__doc__, run_experiments),
                            phases=("experiment_bridge",))

        def complete():
            if terminal_error is not None:
                raise terminal_error
            if blocker is not None:
                raise blocker
            if (self._project / 'experiments/manifest.json').exists():
                self._refresh_experiment_tracker(state, self._experiment_manifest())
            self._validate_required_artifacts(["refine-logs/EXPERIMENT_RESULTS.md",
                                               "refine-logs/EXPERIMENT_TRACKER.md"])

        try:
            result = self._go(
                f"{prompt}\n负责完整实验流程：实现代码、调用 run_experiments、收集结果并核对计划中的每个必做项，"
                "然后交接给 paper_plan。使用 skill(name='run-experiment') 读取执行子技能。不要通过 bash 启动训练。"
                "复用已完成任务；缺少后处理产物时补充后处理，不要重复训练。结束前按 D/B 块撰写 "
                "refine-logs/EXPERIMENT_RESULTS.md。tracker 由运行器生成，不要编辑。核对完整原始计划，"
                "不要只检查 manifest 任务或汇总表。\n"
                + EXPERIMENT_INTERFACE,
                phase="experiment_bridge", completion_check=complete)
        except Exception:
            if terminal_error is not None:
                raise terminal_error
            if blocker is not None:
                raise blocker
            raise
        complete()
        state.experiment_feedback = None
        return NodeResult("refine-logs/EXPERIMENT_RESULTS.md", {
            "phase": "experiment_bridge", "iterations": result.iterations,
            "manifest": "experiments/manifest.json",
            "completed_jobs": len(state.experiment_completed_receipts),
        })


    def _refresh_experiment_tracker(self, state, manifest):
        # Reconcile against current jobs and valid artifacts, including durable receipts
        # from an earlier workflow process. Changed prerequisites invalidate dependents.
        receipts = list(state.experiment_completed_receipts)
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
    def _experiment_manifest(self) -> dict[str, Any]:
        self._require_file("experiments/manifest.json")
        return self.experiments.load_manifest()


    @property
    def _project(self) -> Path:
        return Path(self.agent.project).resolve()

    def _require_file(self, relative: str) -> Path:
        path = self._project / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactValidationError(f"required artifact is missing or empty: {relative}")
        return path

    def _validate_required_artifacts(self, relatives: list[str]) -> None:
        for relative in relatives:
            path = self._require_file(relative)
            if not path.read_text(encoding="utf-8").strip():
                raise ArtifactValidationError(f"required artifact is empty: {relative}")

    def _inspect_project_artifacts(self, relatives: list[str]) -> list[dict[str, Any]]:
        records = []
        for relative in relatives:
            path = self._project / relative
            record: dict[str, Any] = {
                "path": relative,
                "exists": path.is_file(),
                "size": path.stat().st_size if path.is_file() else 0,
            }
            if path.is_file():
                try:
                    record["preview"] = path.read_text(encoding="utf-8")[:1000]
                except UnicodeDecodeError:
                    record["binary"] = True
            records.append(record)
        return records

    def _result_files(self) -> list[Path]:
        root = self._project / "results"
        if not root.is_dir():
            return []
        files = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".json", ".csv"}:
                continue
            try:
                if path.suffix.lower() == ".json":
                    data = json.loads(path.read_text(encoding="utf-8"))
                    valid = self._has_numeric_measurement(data)
                else:
                    with path.open(newline="", encoding="utf-8") as stream:
                        rows = list(csv.DictReader(stream))
                    valid = self._has_numeric_measurement(rows)
            except (OSError, ValueError, UnicodeError):
                continue
            if valid:
                files.append(path)
        return files

    @staticmethod
    def _has_numeric_measurement(value: Any) -> bool:
        if isinstance(value, dict):
            return any(WorkflowRunner._has_numeric_measurement(item) for item in value.values())
        if isinstance(value, list):
            return any(WorkflowRunner._has_numeric_measurement(item) for item in value)
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                return False
        return False

    def _run_paper_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        results = self._result_files()
        if not results:
            raise AwaitingResults("No numeric CSV/JSON experiment results in results/. Run the experiments, then resume with the same state.")
        self._require_file("refine-logs/FINAL_PROPOSAL.md")
        self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        paths = [str(path.relative_to(self._project)) for path in results]
        result = self._go(
            f"原始任务：\n{prompt}\n\n制定论文计划。可用的实测结果文件：{paths}。"
             , phase="paper_plan")
        self._require_file("PAPER_PLAN.md")
        return NodeResult("PAPER_PLAN.md", {"phase": "paper_plan", "results": paths, "iterations": result.iterations})

    def _run_paper_figure(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        if not self._result_files():
            raise AwaitingResults("Experiment results are missing from results/.")
        result = self._go(
            f"原始任务：\n{prompt}\n\n使用当前阶段的 skill，根据 PAPER_PLAN.md 和 results/ 制作计划中的图。"
            "你负责视觉检查和修复：使用 view_image 查看每张最终图，修复排版问题，修改后再次查看。"
            "按照 skill 的要求，将最终图文件及预览的哈希值和具体检查发现写入 figures/visual_checks.json。"
            'Figure 1 启用时，在 figures/figure_manifest.json 声明最终交付路径：'
            '{"figure_1":{"asset":"figures/自选文件名.png","preview":"figures/自选文件名.png"}}。'
            "生成图的 asset 和 preview 均为 generate_image 实际输出的 PNG；文件名可自选。"
            "在 latex_includes.tex 引用 asset，并用 view_image 检查该 preview。"
            "代码绘图回退时 asset 为 PDF，preview 为 PNG，回退清单使用相同路径。"
            "完成检查会拒绝缺失或过期的视觉审查记录；不要将此工作留给外部监督者。", phase="paper_figure")
        self._require_file("figures/latex_includes.tex")
        self._require_file("figures/figure_plan.md")
        metadata: dict[str, Any] = {"phase": "paper_figure", "iterations": result.iterations}
        images = self._figure_receipts(getattr(result, "messages", []))
        if images:
            backends = {image.get("backend", "project-mcp") for image in images.values()}
            metadata.update(image_backend=next(iter(backends)) if len(backends) == 1 else "mixed",
                            images=list(images.values()))
        metadata["visual_checks"] = self._figure_visual_checks(getattr(result, "messages", []))
        return NodeResult("figures/latex_includes.tex", metadata)

    @artifact_contract
    def _figure_visual_checks(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Require current-invocation visual input for every delivered graphic.

        This checks review provenance/freshness, not the model's aesthetic judgment.
        """
        figures = (self._project / "figures").resolve()

        def local_file(relative: str) -> Path:
            if not isinstance(relative, str):
                raise ArtifactValidationError("Visual check paths must be strings")
            path = (self._project / relative).resolve()
            if not path.is_relative_to(figures) or not path.is_file():
                raise ArtifactValidationError(f"Visual check needs an existing file inside figures/: {relative}")
            return path

        assets: set[Path] = set()
        visited: set[Path] = set()

        def collect(source: Path):
            if source in visited:
                return
            visited.add(source)
            text = re.sub(r"(?<!\\)%[^\n]*", "", source.read_text(encoding="utf-8"))
            for relative in re.findall(r"\\includegraphics\*?(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", text):
                path = self._project / relative
                if not path.suffix:
                    path = next((path.with_suffix(ext) for ext in (".pdf", ".png", ".jpg", ".jpeg")
                                 if path.with_suffix(ext).is_file()), path)
                assets.add(local_file(str(path)))
            for relative in re.findall(r"\\(?:input|include)\s*\{([^{}]+)\}", text):
                path = self._project / relative
                if not path.suffix:
                    path = path.with_suffix(".tex")
                collect(local_file(str(path)))

        collect(self._require_file("figures/latex_includes.tex"))
        if not assets:
            return []
        review_path = self._require_file("figures/visual_checks.json")
        reviews = json.loads(review_path.read_text(encoding="utf-8")).get("figures")
        if not isinstance(reviews, list):
            raise ArtifactValidationError("visual_checks.json requires a figures list")
        viewed = set()
        for message in messages:
            if (message.get("role") == "tool" and message.get("name") == "view_image"
                    and not message.get("is_error")):
                receipt = json.loads(message.get("content", "{}"))
                # Earlier drafts may have been removed; reference images may live
                # elsewhere in the project. Only final review entries need exist.
                viewed.add((str((self._project / receipt["path"]).resolve()), receipt["sha256"]))
        checked = {}
        for review in reviews:
            asset = local_file(review["asset"])
            preview = local_file(review["preview"])
            if asset in checked:
                raise ArtifactValidationError(f"Duplicate visual check: {review['asset']}")
            if preview.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                raise ArtifactValidationError("Visual check preview must be PNG/JPEG")
            for path, key in ((asset, "asset_sha256"), (preview, "preview_sha256")):
                if hashlib.sha256(path.read_bytes()).hexdigest() != review.get(key):
                    raise ArtifactValidationError(f"Figure changed after visual review: {path.name}; render and view again")
            if (str(preview), review["preview_sha256"]) not in viewed:
                raise ArtifactValidationError(f"View the final preview with view_image in this invocation: {review['preview']}")
            if (review.get("status") != "passed" or not isinstance(review.get("findings"), str)
                    or not review["findings"].strip()):
                raise ArtifactValidationError(f"Visual review is incomplete: {review['asset']}; inspect and repair before finishing")
            checked[asset] = review
        missing = assets - checked.keys()
        if missing:
            raise ArtifactValidationError("Missing visual checks for: " + ", ".join(sorted(p.name for p in missing)))
        return [checked[path] for path in sorted(assets)]

    @artifact_contract
    def _figure_receipts(self, messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        # Only receipts returned by this invocation count. The runner never generates.
        reviews = self._figure_visual_checks(messages)
        delivered = {str((self._project / review['asset']).resolve().relative_to(self._project))
                     for review in reviews}
        figure_one = None
        if self.require_image_figure:
            declaration = self._project / 'figures/figure_manifest.json'
            if not declaration.is_file():
                raise ArtifactValidationError(
                    'Declare final Figure 1 asset and preview in figures/figure_manifest.json: '
                    '{"figure_1":{"asset":"figures/your-name.png","preview":"figures/your-name.png"}}. '
                    'Reuse the existing generated image; do not regenerate it to change its filename.')
            figure_one = json.loads(declaration.read_text(encoding='utf-8')).get('figure_1')
            if not isinstance(figure_one, dict):
                raise ArtifactValidationError('figure_manifest.json requires a figure_1 object with asset and preview')
            figure_one = dict(figure_one)
            for key in ('asset', 'preview'):
                relative = figure_one.get(key)
                if not isinstance(relative, str):
                    raise ArtifactValidationError(f'Figure 1 declaration requires {key}')
                path = (self._project / relative).resolve()
                if not path.is_relative_to(self._project / 'figures'):
                    raise ArtifactValidationError(f'Figure 1 {key} must be inside project figures/')
                figure_one[key] = str(path.relative_to(self._project))
            if figure_one['asset'] not in delivered:
                raise ArtifactValidationError(
                    f"Final Figure 1 is not referenced in latex_includes.tex: {figure_one['asset']}")
            if not any(str((self._project / r['asset']).resolve().relative_to(self._project)) == figure_one['asset']
                       and str((self._project / r['preview']).resolve().relative_to(self._project)) == figure_one['preview']
                       for r in reviews):
                raise ArtifactValidationError('Figure 1 declaration does not match its final visual review preview')
        images: dict[str, dict[str, Any]] = {}
        for message in messages:
            if (message.get("role") != "tool" or message.get("name") != "generate_image"
                    or message.get("is_error")):
                continue
            job = json.loads(message.get("content", "{}"))
            if not isinstance(job, dict) or job.get("status") != "completed":
                raise ArtifactValidationError("Invalid generate_image success receipt")
            relative = job.get("output_path")
            if not isinstance(relative, str):
                raise ArtifactValidationError("Image receipt has no output_path")
            image_path = (self._project / relative).resolve()
            if not image_path.is_relative_to(self._project / "figures"):
                raise ArtifactValidationError("Image receipt output must be inside project figures/")
            relative = str(image_path.relative_to(self._project))
            if relative in delivered:
                images[relative] = job
        for relative, job in images.items():
            data = self._require_file(relative).read_bytes()
            if len(data) < 100 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ArtifactValidationError(f"Image receipt output is not a PNG: {relative}")
            if hashlib.sha256(data).hexdigest() != job.get("sha256"):
                raise ArtifactValidationError(f"Image changed after generate_image: {relative}")
        if figure_one is not None:
            expected = figure_one['preview']
            if figure_one['asset'] == expected and expected in images:
                return images
            from paper_agent.tools.image_generation import IMAGE_FAILURE_LIMIT, image_generation_state
            state = image_generation_state(self._project)
            if len(state["failures"]) < IMAGE_FAILURE_LIMIT:
                raise ArtifactValidationError(
                    f"Final Figure 1 requires a successful generate_image tool receipt from this invocation "
                    f"for its declared PNG asset: {figure_one['asset']}; preview: {expected}. "
                    "Declare the existing generated PNG as both asset and preview to reuse it.")
            images[expected] = self._code_figure_receipt(figure_one, state, messages)
        return images

    def _code_figure_receipt(self, figure_one: dict, state: dict,
                             messages: list[dict[str, Any]]) -> dict[str, Any]:
        expected = figure_one['preview']
        manifest = json.loads(self._require_file("figures/figure_1_fallback.json").read_text(encoding="utf-8"))
        if (manifest.get("backend") != "code" or manifest.get("output_path") != expected
                or manifest.get('pdf_path') != figure_one['asset']):
            raise ArtifactValidationError("Figure 1 fallback paths must match figure_manifest.json asset and preview; backend must be code")
        figures = (self._project / "figures").resolve()
        for key, hash_key, suffix in (("source_path", "source_sha256", ".py"),
                                      ("pdf_path", "pdf_sha256", ".pdf"),
                                      ("output_path", "sha256", ".png")):
            relative = manifest.get(key)
            if not isinstance(relative, str):
                raise ArtifactValidationError(f"Figure 1 fallback requires {key}")
            path = (self._project / relative).resolve()
            if not path.is_relative_to(figures) or path.suffix != suffix:
                raise ArtifactValidationError(f"Figure 1 fallback {key} must be a {suffix} file inside figures/")
            data = self._require_file(relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest.get(hash_key):
                raise ArtifactValidationError(f"Figure 1 fallback hash mismatch: {key}")
            if key == "output_path" and (len(data) < 100 or not data.startswith(b"\x89PNG\r\n\x1a\n")):
                raise ArtifactValidationError("Figure 1 fallback output is not a PNG")
            if key == "pdf_path" and not data.startswith(b"%PDF-"):
                raise ArtifactValidationError("Figure 1 fallback output is not a PDF")
        reviews = self._figure_visual_checks(messages)
        if not any(review["asset"] == manifest["pdf_path"] and review["preview"] == expected
                   for review in reviews):
            raise ArtifactValidationError("Include the Figure 1 fallback PDF in latex_includes.tex and visually review its PNG preview")
        return {**manifest, "status": "completed", "failure_count": len(state["failures"]),
                "reason": state["failures"][-1]["error"]}

    def _run_paper_write(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        self._require_file("figures/latex_includes.tex")
        result = self._go(
            f"{prompt}\n根据 PAPER_PLAN.md 和已验证的 results/ 撰写论文。仅使用已生成的图和已核实的引用。"
            "写入 paper/main.tex、paper/sections/*.tex 和 paper/references.bib。尽可能保留已有论文内容。", phase="paper_write")
        self._require_file("paper/main.tex")
        self._require_file("paper/references.bib")
        if not list((self._project / "paper/sections").glob("*.tex")):
            raise RuntimeError("paper/sections/ has no TeX sections")
        return NodeResult("paper/main.tex", {"phase": "paper_write", "iterations": result.iterations})

    def _compile_pdf(self) -> Path:
        source = self._require_file("paper/main.tex")
        if shutil.which("latexmk") is None:
            raise RuntimeError("latexmk is not installed; install a LaTeX distribution before paper_compile")
        pdf = source.with_suffix(".pdf")
        completed = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
            cwd=source.parent, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"latexmk failed: {(completed.stdout + completed.stderr)[-3000:]}")
        if not pdf.is_file() or pdf.stat().st_size < 100:
            raise RuntimeError("latexmk completed but paper/main.pdf is missing or invalid")
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise RuntimeError("latexmk completed but paper/main.pdf is not a PDF")
        return pdf

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
        from .review_runner import ReviewRequest, ReviewRunner

        paper = self._require_file("paper/main.tex").parent
        self._require_file("paper/main.pdf")
        log = paper / "PAPER_IMPROVEMENT_LOG.md"
        from .paper_review_inputs import paper_review_inputs

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
                if round_number == 2:
                    inputs.update((Path("paper/review_round1.md"), Path("paper/PAPER_IMPROVEMENT_LOG.md")))
                instructions = (
                    "第一轮完整审稿：独立评审当前论文源码和 PDF，不读取旧审稿报告和改进日志。"
                    "目标在15–20分钟内完成。给出十分制评分、优点、按 CRITICAL/MAJOR/MINOR 分级的问题、"
                    "修改建议及投稿就绪判断。检查科学逻辑、证据、引用、图与图注及排版。"
                    if round_number == 1 else
                    "第二轮修改验收：读取第一轮报告和改进日志，对第一轮问题逐项标记已解决、部分解决、"
                    "未解决并给出证据；检查修改引入的新问题，不重新开展整篇独立审稿。目标在5–10分钟内完成。"
                    "给出剩余问题的严重级别、建议及投稿就绪判断。"
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
                    "遵循 auto-paper-improvement-loop 技能。只使用已有实验和可核实引用，"
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

    def _next_node(self, state: WorkflowState, node: str) -> str | None:
        if node == "literature":
            return "data_search"
        if node == "data_search":
            return "ideas"
        if node == "ideas":
            return "refine"
        if node == "refine":
            return "experiment_plan"
        if node == "experiment_plan":
            return "experiment_bridge"
        if node == "experiment_bridge":
            return "paper_plan"
        if node == "paper_plan":
            return "paper_figure"
        if node == "paper_figure":
            return "paper_write"
        if node == "paper_write":
            return "paper_compile"
        if node == "paper_compile":
            return "improvement"
        if node == "improvement":
            return None
        return None

    @staticmethod
    def _fail(state: WorkflowState, message: str) -> WorkflowState:
        state.status = "failed"
        state.error = message
        return state


__all__ = ["WorkflowConfig", "WorkflowRunner", "WorkflowState"]
