"""Deterministic workflow controller for research and paper production."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import math
import logging
from batteryscientist.tools.experiments.queue import run_queue
from batteryscientist.tools.experiments.executor import ExperimentExecutor, AwaitingResults, receipt_matches_job, job_dependencies
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .loop import ArtifactValidationError, PhaseAgent
from .stages import NEXT_NODE
from batteryscientist.tools import ToolContext, ToolResult, ToolSpec
from batteryscientist.tools.experiments.plan import read_plan_blocks, write_experiment_tracker
from batteryscientist.tools.paper_compile import compile_pdf


WorkflowStatus = Literal["running", "awaiting_results", "awaiting_user", "completed", "failed"]
PaperStatus = Literal["pending", "pdf_ready"]


logger = logging.getLogger(__name__)


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
    """Default entry, interaction mode and stage-local paper review settings."""

    start_node: str = "literature"
    improvement_start_round: int = 1
    paper_review_timeout: float = 1800
    paper_review_round2_timeout: float = 1200
    improvement_resume: bool = False
    mode: Literal["auto", "expert"] = "auto"


@dataclass
class NodeResult:
    """Output returned by a workflow node handler."""

    files: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    final: str = ""

    @property
    def artifact(self) -> str:
        return self.files[0] if self.files else ""


class WorkflowRunner:
    """Route research stages through role sessions hosted by :class:`PhaseAgent`.

    Nodes are ordinary Python methods with explicit, testable transitions.
    """

    def __init__(self, agent: PhaseAgent, *, config: WorkflowConfig | None = None,
                 run_id: str | None = None,
                 on_node_start: Callable[[str, WorkflowState], None] | None = None,
                 on_node_end: Callable[[str, WorkflowState, NodeResult], None] | None = None,
                 on_node_progress: Callable[[str, WorkflowState, dict[str, Any]], None] | None = None):
        self.agent = agent
        self.experiments = ExperimentExecutor(agent.project, knowledge_root=getattr(agent, "knowledge_root", None))
        self.config = config or WorkflowConfig()
        if self.config.mode not in {"auto", "expert"}:
            raise ValueError("mode must be auto or expert")
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

    def execute_node(self, node: str, prompt: str, *,
                     state: WorkflowState | None = None, instruction: str = "",
                     execution: dict[str, Any] | None = None,
                     approve_revision: bool = False) -> WorkflowState:
        """Execute exactly one prepared node; downstream dispatch belongs to main."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        state = state or self.start_from_project(node, prompt)
        if state.current_node != node:
            raise ValueError("Prepared state does not match the assigned node")
        if state.project_goal and state.project_goal != prompt:
            raise ValueError("项目总目标不能由阶段指令覆盖；请单独传入 instruction")
        state.project_goal = prompt
        if state.status == "awaiting_results":
            state.status, state.error = "running", None
        if state.status != "running":
            return state
        self.agent.interaction_mode = state.mode
        state, result = self._execute_current_node(
            state, instruction=instruction, approve_revision=approve_revision)
        if execution:
            state.node_results.setdefault(node, {})["execution"] = execution
        if self.on_node_end and state.status in {"completed", "awaiting_user"}:
            self.on_node_end(node, state, result)
        return state

    def _execute_current_node(self, state: WorkflowState, *, instruction: str = "",
                              approve_revision: bool = False) -> tuple[WorkflowState, NodeResult | None]:
        node = state.current_node
        handler = self._nodes[node]
        if state.failed_node is not None:
            state.fault_retry_count += 1
            state.failed_node = None
        self.agent.last_state = None
        try:
            if self.on_node_start:
                self.on_node_start(node, state)
            node_prompt = state.project_goal
            if instruction:
                node_prompt = f"项目总目标：\n{state.project_goal}\n\n当前阶段指令（仅适用于 {node}）：\n{instruction}"
            evidence = {name: [ref['path'] for ref in refs] for name, refs in state.artifacts.items()}
            if evidence:
                node_prompt += "\n已有交付路径（按需读取，不代表内容已核实）：\n" + json.dumps(evidence, ensure_ascii=False)
            review_required = state.mode == "expert"
            result = handler(state, node_prompt)
            files = list(dict.fromkeys(result.files))
            for name in files:
                self._require_file(name)
            result.files = files
            selected_report = files[0] if files else None
            selected_report = selected_report if state.mode == 'auto' and node in {'refine', 'experiment_plan'} else None
            selected_sha = (hashlib.sha256(self._require_file(selected_report).read_bytes()).hexdigest()
                            if selected_report else None)
            if review_required:
                checkpoint = {'summary': '阶段产物已生成，请审阅。',
                              'questions': []}
                if not files:
                    raise ValueError('人工审批需要本次交付文件，请登记后再完成')
                report = self._require_file(files[0])
                state.expert_checkpoint = {
                    **checkpoint, "node": node, "next_node": self._next_node(node),
                    "report": files[0],
                    "files": files, "status": "pending",
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "stop_after": state.workflow_stop_after,
                }
        except AwaitingResults as exc:
            state.status = "awaiting_results"
            state.error = str(exc)
            return state, None
        except Exception as exc:
            logger.exception("Workflow node %s failed", node)
            state.failed_node = node
            return self._fail(state, f"node {node} failed: {exc}"), None
        state.artifacts[node] = self._artifact_refs(result.files)
        state.node_results[node] = {
            **result.metadata,
            "instruction": instruction,
            "final": result.final,
            "files": result.files,
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
        state.status = "awaiting_user" if review_required else "completed"
        if review_required and approve_revision and node != "refine":
            state = self.resume_checkpoint(state, advance=False)
        return state, result

    def resume_checkpoint(self, state: WorkflowState, *, revise: bool = False,
                          proposal_review_sha: str | None = None,
                          advance: bool = True) -> WorkflowState:
        """Approve current files, or prepare to revise the paused node."""
        checkpoint = state.expert_checkpoint
        if (state.status != "awaiting_user" or state.current_node not in self._nodes
                or not checkpoint or checkpoint.get("status") != "pending"
                or checkpoint.get("node") != state.current_node):
            raise ValueError("没有待确认的专家检查点")
        proposal_review = state.current_node == 'refine'
        if not revise:
            node = state.current_node
            review_receipt = {}
            if proposal_review_sha:
                if not proposal_review:
                    raise ValueError("方案审阅批准只能用于研究精炼的专家检查点")
                from batteryscientist.proposal_review import require_candidate
                require_candidate(self._project, proposal_review_sha)
            files = checkpoint.get('files', [checkpoint['report']])
            for relative in files:
                self._require_file(relative)
            if proposal_review_sha:
                from batteryscientist.proposal_review import publish_candidate
                review_receipt = publish_candidate(self._project, proposal_review_sha)
            report = self._require_file(checkpoint["report"])
            state.artifacts[node] = self._artifact_refs(files)
            checkpoint["confirmed_files"] = {
                relative: hashlib.sha256(self._require_file(relative).read_bytes()).hexdigest()
                for relative in files
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
            if not advance or checkpoint.get("stop_after") == node or next_node is None:
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
            completion_check: Callable[[], None] | None = None,
            prepare_completion: Callable[[], None] | None = None):
        options = {"prepare_completion": prepare_completion} if prepare_completion is not None else {}
        return self.agent.go(prompt, phase=phase, completion_check=completion_check, **options)

    def _agent_node(self, prompt: str, phase: str, **metadata) -> NodeResult:
        result = self._go(prompt, phase=phase)
        files = list(getattr(result, 'files', []))
        return NodeResult(files, {'phase': phase, 'iterations': result.iterations, **metadata},
                          getattr(result, 'final', ''))

    def start_from_project(self, node: str, prompt: str,
                           state: WorkflowState | None = None) -> WorkflowState:
        """Prepare an explicit entry point; main decides whether project evidence is ready."""
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        if state is None:
            state = WorkflowState(self.run_id, project_goal=prompt, mode=self.config.mode)
        if state.project_goal and state.project_goal != prompt:
            raise ValueError("项目总目标不能由阶段指令覆盖；请单独传入 instruction")
        state.project_goal = prompt
        state.current_node = node
        state.expert_checkpoint = None
        state.status = "running"
        state.error = None
        return state

    def _run_data_search(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'data_search')

    def _run_ideas(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'ideas')

    def _run_literature(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'literature')

    def _run_refine(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'research_refine')

    def _run_experiment_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        # Protect exactly the approved proposal version, regardless of its filename.
        approved = next((item for item in reversed(state.history)
                         if item.get('event') == 'expert_approved' and item.get('node') == 'refine'), None)
        if state.mode == 'expert' and approved:
            report = self._require_file(approved['report'])
            before = report.read_bytes()
            if hashlib.sha256(before).hexdigest() != approved['sha256']:
                raise ValueError('当前版本方案尚未批准，请审阅新版后确认')
        result = self._agent_node(prompt, 'experiment_plan')
        if state.mode == 'expert' and approved and report.read_bytes() != before:
            raise ValueError('实验规划期间改写了已批准方案')
        return result

    def _run_experiment_bridge(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Own implementation, deployment, collection and plan reconciliation."""
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
            statuses = self._refresh_experiment_tracker(state, self._experiment_manifest(state))
            completed_jobs = sum(item['status'] == 'completed' for item in statuses.values())

        result = self._go(prompt, phase='experiment_bridge', completion_check=complete)
        files = list(getattr(result, 'files', []))
        return NodeResult(files, {
            'phase': 'experiment_bridge', 'iterations': result.iterations,
            'manifest': 'experiments/manifest.json', 'completed_jobs': completed_jobs,
        }, getattr(result, 'final', ''))


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

    def _experiment_manifest(self, state=None) -> dict[str, Any]:
        self._require_file("experiments/manifest.json")
        try:
            return self.experiments.load_manifest()
        except ValueError as exc:
            raise ArtifactValidationError(str(exc)) from exc


    @property
    def _project(self) -> Path:
        return Path(self.agent.project).resolve()

    def _require_file(self, relative: str) -> Path:
        path = (self._project / relative).resolve()
        if not path.is_relative_to(self._project) or not path.is_file():
            raise ArtifactValidationError(f"交付或操作所需文件不存在或位于项目外：{relative}")
        return path

    def _artifact_refs(self, files: Iterable[str]) -> list[dict[str, Any]]:
        return [{'path': name, 'bytes': self._require_file(name).stat().st_size} for name in files]

    def _available_experiment_outputs(self, state) -> list[Path]:
        manifest = self._experiment_manifest(state)
        statuses = self._refresh_experiment_tracker(state, manifest)
        return [path for job in manifest['jobs']
                if statuses[job['id']]['status'] == 'completed'
                for output in job['expected_outputs']
                if (path := self.experiments.output_path(output['path'])).is_file()]

    def _run_paper_plan(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'paper_plan')

    def _run_paper_figure(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'paper_figure')

    def _run_paper_write(self, state: WorkflowState, prompt: str) -> NodeResult:
        return self._agent_node(prompt, 'paper_write')

    def _compile_pdf(self) -> Path:
        self._require_file("paper/main.tex")
        pdf = compile_pdf(self._project)
        from batteryscientist.tools.paper_citations import validate_body_citations
        try:
            validate_body_citations(self._project, compiled=True)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return pdf

    def _prepare_compiled_pdf(self) -> None:
        try:
            self._compile_pdf()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            raise ArtifactValidationError(str(exc)) from exc

    def _run_paper_compile(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("paper/main.tex")
        result = None
        if (state.mode == "expert" and state.expert_checkpoint
                and state.expert_checkpoint.get("node") == "paper_compile"
                and state.expert_checkpoint.get("status") == "revision_requested"):
            result = self._go(prompt, phase="paper_compile",
                              prepare_completion=self._prepare_compiled_pdf)
        try:
            pdf = self._compile_pdf()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            result = self._go(
                f"{prompt}\n修复 paper/ 中的 LaTeX 编译。读取编译日志，修复源码或安装缺失依赖，并验证编译。"
                f"错误：\n{exc}",
                phase="paper_compile", prepare_completion=self._prepare_compiled_pdf)
            pdf = self._require_file("paper/main.pdf")
        files = list(dict.fromkeys([str(pdf.relative_to(self._project)), "paper/main.tex",
                                   *getattr(result, 'files', [])]))
        metadata = {"phase": "paper_compile"}
        if result is not None:
            metadata['iterations'] = result.iterations
        return NodeResult(files, metadata, getattr(result, 'final', '论文编译完成。'))

    def _run_improvement(self, state: WorkflowState, prompt: str) -> NodeResult:
        from batteryscientist.integrations.review import ReviewRequest, ReviewRunner

        paper = self._require_file("paper/main.tex").parent
        self._require_file("paper/main.pdf")
        log = paper / "PAPER_IMPROVEMENT_LOG.md"
        from batteryscientist.integrations.review import paper_review_inputs

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
        revision_files = []
        compiled_files = []
        final = "论文两轮审稿、修订和编译完成。"
        for round_number in range(start_round, 3):
            report = Path(f"paper/review_round{round_number}.md")
            if stage == "completed":
                break
            if stage not in {"revision", "compile"}:
                inputs = set(paper_review_inputs(self._project))
                timeout = (self.config.paper_review_timeout if round_number == 1
                           else self.config.paper_review_round2_timeout)
                instructions = (Path(__file__).resolve().parents[1] / "skills" /
                                "auto-paper-improvement-loop" / "paper-review.md").read_text(encoding="utf-8")
                progress(round_number, "review", timeout_seconds=timeout,
                         stage_started_at=datetime.now(timezone.utc).isoformat())
                review = ReviewRunner(self._project, timeout=timeout).run(ReviewRequest(
                    kind="paper", instructions=(f"用户研究任务：{prompt}\n当前审稿轮次：{round_number}/2\n"
                                                + instructions),
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
                    f"{prompt}\n当前修改轮次：{round_number}/2；本轮审稿报告：{report}。"
                    "运行器已完成本轮独立评审；本次只修订，不重复发起评审。"
                    "本轮完成后由运行器重新编译。",
                    phase="improvement")
                iterations = result.iterations
                revision_files = list(getattr(result, 'files', []))
                final = getattr(result, 'final', final)
                progress(round_number, "compile")
            compiled = self._run_paper_compile(state, prompt)
            compiled_files = compiled.files
            shutil.copy2(self._project / compiled.artifact, paper / f"main_round{round_number}.pdf")
            rounds.append({"round": round_number, "review": str(report),
                           "iterations": iterations})
            progress(round_number + 1 if round_number == 1 else 2,
                     "review" if round_number == 1 else "completed")
            stage = "review"
        files = list(dict.fromkeys([
            "paper/main.pdf", "paper/main.tex", "paper/PAPER_IMPROVEMENT_LOG.md",
            "paper/review_round1.md", "paper/review_round2.md",
            *revision_files, *compiled_files,
        ]))
        return NodeResult(files, {"phase": "improvement", "rounds": rounds}, final)

    def _next_node(self, node: str) -> str | None:
        return NEXT_NODE.get(node)

    @staticmethod
    def _fail(state: WorkflowState, message: str) -> WorkflowState:
        state.status = "failed"
        state.error = message
        return state


__all__ = ["WorkflowConfig", "WorkflowRunner", "WorkflowState"]
