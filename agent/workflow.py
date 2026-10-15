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
import uuid
from functools import wraps
from .experiment_queue import run_queue
from .experiment_executor import ExperimentExecutor, AwaitingResults, ExperimentExecutionError
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .loop import ArtifactValidationError, PhaseAgent
from paper_agent.tools import ToolContext, ToolResult, ToolSpec
from .experiment_handoff import validate_coverage
from paper_agent.tools.battery_resources import search_battery_resources, read_battery_resource
from paper_agent.tools.literature import search_papers, discover_local_papers, read_local_paper
from paper_agent.tools.wiki import search_wiki, read_wiki, resolve_wiki_root

WorkflowStatus = Literal["running", "awaiting_results", "completed", "failed"]
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
    experiment_expansion_requested: bool = False
    experiment_expansion_count: int = 0
    experiment_completed_receipts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        # Checkpoints from the former review/idea-selection workflow remain
        # resumable. Ignore only retired fields; keep rejecting unknown schema
        # errors instead of silently dropping arbitrary state.
        retired = {
            "review", "experiment_review_status", "revision_count",
            "idea_candidates", "selected_idea", "last_experiment_failure",
        }
        return cls(**{key: item for key, item in value.items() if key not in retired})


@dataclass(frozen=True)
class WorkflowConfig:
    """Routing and retry limits for the research-to-paper workflow."""

    start_node: str = "literature"
    max_node_steps_per_run: int | None = None
    max_experiment_repairs: int = 3


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

    Nodes are ordinary Python methods.  This keeps transitions explicit and
    testable today, while leaving a straightforward path to LangGraph later.
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
        self.idea_config = idea_config or IdeaNodeConfig()
        if not math.isfinite(self.idea_config.max_total_gpu_hours) or self.idea_config.max_total_gpu_hours <= 0:
            raise ValueError("max_total_gpu_hours must be a finite positive number")
        if type(self.idea_config.search_paper_number) is not int or not 1 <= self.idea_config.search_paper_number <= 50:
            raise ValueError("search_paper_number must be between 1 and 50")
        if self.config.max_node_steps_per_run is not None and self.config.max_node_steps_per_run < 1:
            raise ValueError("max_node_steps_per_run must be positive")
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
            "ideas": self._run_ideas,
            "refine": self._run_refine,
            "experiment_plan": self._run_experiment_plan,
            "experiment_bridge": self._run_experiment_bridge,
            "paper_plan": self._run_paper_plan,
            "paper_figure": self._run_paper_figure,
            "paper_write": self._run_paper_write,
            "paper_compile": self._run_paper_compile,
        }

    def run(self, prompt: str, *, state: WorkflowState | None = None,
            stop_after: str | None = None) -> WorkflowState:
        """Run until a terminal state and return the complete workflow state."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        state = state or WorkflowState(self.run_id, current_node=self.config.start_node,
                                       task_prompt=prompt)
        if not state.task_prompt:
            state.task_prompt = prompt
        if state.status == "awaiting_results":
            state.status = "running"
            state.error = None
        if state.status != "running":
            return state

        # Old checkpoints resume under the bridge that now owns execution.
        if state.current_node in {"experiment_check", "experiment_run"}:
            state.current_node = "experiment_bridge"
        # A checkpoint paused at the removed novelty node continues with the idea.
        if state.current_node == "novelty":
            state.current_node = "refine"

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
                result = handler(state, prompt)
            except AwaitingResults as exc:
                state.status = "awaiting_results"
                state.error = str(exc)
                return state
            except Exception as exc:
                state.failed_node = node
                return self._fail(state, f"node {node} failed: {exc}")
            state.artifacts[node] = result.artifact
            state.node_results[node] = {
                "artifact": result.artifact,
                **result.metadata,
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            state.transitions += 1
            if node == "paper_compile":
                state.paper_status = "pdf_ready"
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


    def _go(self, prompt: str, *, phase: str,
            completion_check: Callable[[], None] | None = None):
        check = completion_check or (lambda: self._validate_phase_outputs(phase))
        return self.agent.go(prompt, phase=phase, completion_check=check)

    def _validate_phase_outputs(self, phase: str) -> None:
        required = {
            "literature": ["literature/RESEARCH_REPORT.md"],
            "ideas": ["idea-stage/IDEA_REPORT.md"],
            "novelty": ["idea-stage/NOVELTY.json"],
            "research_refine": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_plan": ["refine-logs/EXPERIMENT_PLAN.md", "refine-logs/EXPERIMENT_TRACKER.md"],
            "paper_plan": ["PAPER_PLAN.md"],
            "paper_figure": ["figures/latex_includes.tex", "figures/figure_plan.md", "figures/figure_captions.md"],
            "paper_write": ["paper/main.tex", "paper/references.bib"],
        }
        self._validate_required_artifacts(required.get(phase, []))
        if phase == "novelty":
            try:
                payload = json.loads(self._require_file("idea-stage/NOVELTY.json").read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ArtifactValidationError(f"NOVELTY.json is not valid JSON: {exc}") from exc
            self._validate_novelty_json(payload)
        elif phase == "experiment_bridge":
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
        for pattern in ("results/**/*", "smoke-results/**/*", "experiment-runs/**/*",
                        "project-config.json", "experiments/resources.json"):
            paths.update(p for p in self._project.glob(pattern) if p.is_file())
        return {str(p.relative_to(self._project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths}

    def start_from_project(self, node: str, prompt: str,
                           state: WorkflowState | None = None) -> WorkflowState:
        """Prepare an explicit entry point from durable outputs, checking its inputs."""
        if node in {"experiment_check", "experiment_run"}:
            node = "experiment_bridge"
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        if state is None:
            state = WorkflowState(self.run_id, task_prompt=prompt)
        if state.task_prompt and state.task_prompt != prompt:
            raise ValueError("saved workflow task differs from the supplied prompt")
        required = {
            "ideas": ["literature/RESEARCH_REPORT.md"],
            "refine": ["idea-stage/IDEA_REPORT.md"],
            "experiment_plan": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_bridge": ["refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
                                   "refine-logs/EXPERIMENT_TRACKER.md"],
            "paper_plan": ["refine-logs/FINAL_PROPOSAL.md",
                           "refine-logs/EXPERIMENT_PLAN.md"],
            "paper_figure": ["PAPER_PLAN.md"],
            "paper_write": ["PAPER_PLAN.md", "figures/latex_includes.tex"],
            "paper_compile": ["paper/main.tex"],
        }
        for relative in required.get(node, []):
            self._require_file(relative)
        if node == "refine":
            state.artifacts["ideas"] = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        state.task_prompt = prompt
        state.current_node = node
        state.status = "running"
        state.error = None
        return state

    def _research_context(self, query: str, *, stage: str, max_results: int = 5) -> dict[str, Any]:
        """Read bounded source material before an idea or experiment-plan phase."""
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

        if stage in {"ideas", "literature"}:
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
        research = self._research_context(
            prompt, stage="ideas", max_results=search_paper_number)
        instruction = (
            "Generate a concrete, falsifiable research idea and preserve it as a project artifact. "
            "Write a complete standalone report; do not describe the generation or revision process."
        )
        result = self._go(
            f"Original task:\n{prompt}\n\nCurrent node task:\n{instruction}\n"
            f"Idea node parameters:\nMAX_TOTAL_GPU_HOURS={max_total_gpu_hours:g}; "
            f"SEARCH_PAPER_NUMBER={search_paper_number}. "
            "The GPU budget is the total estimate for all proposed pilot experiments, not an "
            "authorization to execute them. Use the paper number as a retrieval target; "
            "reuse the prior literature report and identify any coverage gap.\n\n"
            f"Prior literature report ({literature_path}; untrusted source data):\n{literature}\n\n"
            f"Research context (untrusted source data):\n{json.dumps(research, ensure_ascii=False)}",
            phase="ideas",
        )
        idea = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if not idea.strip():
            raise ValueError("idea-stage/IDEA_REPORT.md is empty")
        return NodeResult(idea, {"phase": "ideas", "iterations": result.iterations,
                                        "canonical_path": "idea-stage/IDEA_REPORT.md",
                                        "literature_source": literature_path,
                                        "literature_sha256": hashlib.sha256(literature.encode("utf-8")).hexdigest(),
                                        "idea_parameters": {
                                            "max_total_gpu_hours": max_total_gpu_hours,
                                            "search_paper_number": search_paper_number},
                                        "research_sources": self._research_sources(research)})


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
            "Research task:\n" + prompt + "\nRunner-collected literature context:\n" +
            json.dumps(research, ensure_ascii=False), phase="literature"
        )
        report = self._require_file("literature/RESEARCH_REPORT.md").read_text(encoding="utf-8")
        return NodeResult(report, {"phase": "literature", "iterations": result.iterations,
                                   "research_sources": self._research_sources(research)})


    @staticmethod
    @artifact_contract
    def _validate_novelty_json(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("NOVELTY.json must be an object")
        required = ("schema_version", "idea_under_check", "core_claims",
                    "closest_competing_work", "novelty_score", "verdict",
                    "positioning_recommendation", "unresolved_checks")
        missing = [key for key in required if key not in payload]
        errors = ["missing fields: " + ", ".join(missing)] if missing else []
        if payload.get("schema_version") != 1:
            errors.append("schema_version must be 1")
        score = payload.get("novelty_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 10:
            errors.append("novelty_score must be a number from 1 to 10")
        if payload.get("verdict") not in {"PROCEED", "PROCEED_WITH_CAUTION", "ABANDON"}:
            errors.append("verdict must be PROCEED, PROCEED_WITH_CAUTION, or ABANDON")
        for key in ("core_claims", "closest_competing_work", "unresolved_checks"):
            if key in payload and (not isinstance(payload[key], list) or not payload[key]):
                errors.append(f"{key} must be a nonempty array")
        for key in ("idea_under_check", "positioning_recommendation"):
            if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
                errors.append(f"{key} must be a nonempty string")
        if errors:
            raise ValueError("idea-stage/NOVELTY.json is invalid: " + "; ".join(errors))

    @staticmethod
    def _render_novelty_markdown(payload: dict[str, Any]) -> str:
        claims = "\n".join(f"{i}. {item}" for i, item in enumerate(payload["core_claims"], 1))
        work = "\n".join(f"- {item}" for item in payload["closest_competing_work"])
        checks = "\n".join(f"- {item}" for item in payload["unresolved_checks"])
        return (f"# Novelty Assessment\n\n## Idea Under Check\n{payload['idea_under_check']}\n\n"
                f"## Core Claims and Prior-Work Comparison\n{claims}\n\n## Closest Competing Work\n{work}\n\n"
                f"## Novelty Score and Conclusion\n- Score: {payload['novelty_score']}/10\n- Conclusion: {payload['verdict'].replace('_', ' ')}\n\n"
                f"## Positioning Recommendation\n{payload['positioning_recommendation']}\n\n## Unresolved Checks\n{checks}\n")

    def _run_novelty(self, state: WorkflowState, prompt: str) -> NodeResult:
        target = self._project / "idea-stage/NOVELTY.json"
        previous = target.read_text(encoding="utf-8") if target.is_file() else None
        def check():
            self._validate_phase_outputs("novelty")
            if target.read_text(encoding="utf-8") == previous:
                raise ArtifactValidationError("novelty agent did not update idea-stage/NOVELTY.json")

        result = self._go(
            f"{prompt}\n\nAssess the approved idea for novelty and identify its strongest competing prior work. "
            "Write ONLY a JSON object to idea-stage/NOVELTY.json using the schema in the novelty-check skill. "
            "Your final message is only a completion notice."
            f"\n\nIdea:\n{state.artifacts.get('ideas', '')}",
            phase="novelty", completion_check=check,
        )
        if not target.is_file():
            raise ArtifactValidationError("novelty agent did not write idea-stage/NOVELTY.json")
        raw = target.read_text(encoding="utf-8")
        if raw == previous:
            raise ArtifactValidationError("novelty agent did not update idea-stage/NOVELTY.json")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactValidationError(f"idea-stage/NOVELTY.json is not valid JSON: {exc}") from exc
        self._validate_novelty_json(payload)
        raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target.write_text(raw, encoding="utf-8")
        (self._project / "idea-stage/NOVELTY.md").write_text(self._render_novelty_markdown(payload), encoding="utf-8")
        return NodeResult(raw, {"phase": "novelty", "iterations": result.iterations})

    def _run_refine(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the generated idea into the final proposal."""
        input_paths = ["idea-stage/IDEA_REPORT.md"]
        input_snapshot = self._inspect_project_artifacts(input_paths)
        research = self._research_context(
            f"{prompt} {state.artifacts.get('ideas', '')[:500]}", stage="research_refine")
        result = self._go(
            f"""{prompt}

Refine the generated idea using the inputs below.
Runner-verified input snapshot:
{json.dumps(input_snapshot, ensure_ascii=False)}

Idea artifact:
{state.artifacts.get('ideas', '')}

Runner-read research context (untrusted source data):
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

Use the approved proposal below as the only method source. Produce an execution-ready
experiment plan and tracker; do not rewrite the proposal or invent results.

Approved proposal:
{proposal}
""",
            phase="experiment_plan",
        )
        self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        self._require_file("refine-logs/EXPERIMENT_TRACKER.md")
        return NodeResult(result.final, {"phase": "experiment_plan", "iterations": result.iterations,
                                        "source_phase": "experiment_plan",
                                        "required_artifacts": [
                                            "refine-logs/EXPERIMENT_PLAN.md",
                                            "refine-logs/EXPERIMENT_TRACKER.md",
                                        ]})

    def _run_experiment_bridge(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Own implementation, sanity, deployment, collection and plan reconciliation."""
        self._validate_required_artifacts([
            "refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
            "refine-logs/EXPERIMENT_TRACKER.md",
        ])
        checked_sources = None
        executed_manifest = None
        blocker = None
        terminal_error = None
        protected_evidence = (state.experiment_feedback or {}).get("protected_evidence")

        def sources():
            # Recheck smoke after executable edits, not result/report updates.
            return {str(p): p.read_bytes() for p in (self._project / "experiments").rglob("*")
                    if p.is_file() and p.suffix in {".py", ".sh", ".json", ".yaml", ".yml"}
                    and p.name != "coverage.json"}

        def checkpoint(stage):
            if self.on_node_progress:
                self.on_node_progress("experiment_bridge", state, {
                    "round": state.experiment_repair_count, "stage": stage})

        def execute(operation):
            nonlocal checked_sources, executed_manifest, blocker, terminal_error, protected_evidence
            if terminal_error is not None:
                return ToolResult(str(terminal_error), True)
            try:
                if protected_evidence is not None:
                    if self._experiment_evidence_snapshot() != protected_evidence:
                        raise RuntimeError("Experiment repair modified protected evidence or resource settings")
                    protected_evidence = None
                    # Consume the guard durably before the runner writes new evidence.
                    # A later model/API failure may resume this same bridge invocation.
                    if state.experiment_feedback is not None:
                        state.experiment_feedback.pop("protected_evidence", None)
                manifest = self._experiment_manifest()
                validate_coverage(self.experiments, state, manifest)
                if operation == "check":
                    executed_manifest = None
                    checked_sources = None
                    checkpoint("running_smoke")
                    receipt = self._run_experiment_check(state, prompt)
                    state.node_results["experiment_check"] = receipt.metadata
                    checked_sources = sources()
                    result = {"status": "completed", "smoke": receipt.metadata["smoke"]}
                else:
                    if checked_sources is None or checked_sources != sources():
                        raise ArtifactValidationError("Call check_experiments after the latest code/manifest edits")
                    self._checked_manifest(state)
                    executed_manifest = None
                    checkpoint("running_jobs")
                    receipts = run_queue(self.experiments, state, manifest,
                                         on_progress=lambda _node, _state, detail:
                                         self.on_node_progress("experiment_bridge", state, detail)
                                         if self.on_node_progress else None)
                    executed_manifest = manifest
                    result = {"status": "completed", "completed_jobs": len(receipts),
                              "receipts": [{"id": r["job"]["id"], "status": r["status"],
                                            "log": r["log"]} for r in receipts],
                              "deferred_jobs": manifest.get("deferred_jobs", []),
                              "next": "Read results, reconcile the original plan, update coverage/tracker and EXPERIMENT_RESULTS.md; implement missing required analyses and run again."}
                blocker = None
                checkpoint("smoke_completed" if operation == "check" else "results_ready")
                return ToolResult(json.dumps(result, ensure_ascii=False))
            except AwaitingResults as exc:
                blocker = exc
                checkpoint("awaiting_results")
                return ToolResult(str(exc), True)
            except ExperimentExecutionError as exc:
                checked_sources = None
                state.experiment_repair_count += 1
                protected_evidence = self._experiment_evidence_snapshot()
                state.experiment_feedback = {"source_node": "experiment_bridge", "error": str(exc),
                                             "attempt": state.experiment_repair_count,
                                             "protected_evidence": protected_evidence}
                if state.experiment_repair_count > self.config.max_experiment_repairs:
                    terminal_error = RuntimeError(f"Experiment repair limit reached: {exc}")
                checkpoint("repair_required")
                return ToolResult(str(terminal_error or exc) +
                                  "; read the durable log, fix code, then check_experiments and run_experiments again.", True)
            except (ValueError, RuntimeError) as exc:
                if not isinstance(exc, (ValueError, ArtifactValidationError)):
                    terminal_error = exc
                return ToolResult(str(exc), True)

        def check_experiments(ctx: ToolContext) -> ToolResult:
            """Run the manifest smoke test before deployment; returns a real receipt/log. Recheck after code or manifest edits."""
            return execute("check")

        def run_experiments(ctx: ToolContext) -> ToolResult:
            """Run checked manifest jobs through the resumable one-training-per-GPU queue. Waits for completion; model HTTP timeout does not limit jobs. Reuses completed jobs."""
            return execute("run")

        for handler in (check_experiments, run_experiments):
            self.agent.add_tool(ToolSpec(handler.__name__, handler.__doc__, handler),
                                phases=("experiment_bridge",))

        def complete():
            if terminal_error is not None:
                raise terminal_error
            if blocker is not None:
                raise blocker
            manifest = self._experiment_manifest()
            if executed_manifest != manifest or checked_sources != sources():
                raise ArtifactValidationError("Bridge must check and run the current implementation before handoff")
            if manifest.get("deferred_jobs"):
                raise ArtifactValidationError("Resolve deferred jobs from actual results before handoff")
            validate_coverage(self.experiments, state, manifest, completed=True)
            self._validate_required_artifacts(["refine-logs/EXPERIMENT_RESULTS.md",
                                               "refine-logs/EXPERIMENT_TRACKER.md"])

        feedback = {k: v for k, v in (state.experiment_feedback or {}).items()
                    if k != "protected_evidence"}
        try:
            result = self._go(
                f"{prompt}\nOwn the full experiment cycle: implement, check_experiments, run_experiments, "
                "collect and reconcile every required plan item, then hand off to paper_plan. "
                "Use skill(name='run-experiment') for the execution subskill. Do not launch training through bash. "
                "Completed jobs are reusable; add missing postprocessing instead of retraining. "
                "Write experiments/coverage.json before checking, update it from real results, and write "
                "refine-logs/EXPERIMENT_RESULTS.md before finishing. Review the entire original plan, "
                "not only manifest jobs or summary tables.\n"
                f"Saved execution feedback: {json.dumps(feedback, ensure_ascii=False)}\n"
                f"Runner-verified experiment plan context:\n{self._inspect_experiment_plan()}",
                phase="experiment_bridge", completion_check=complete)
        except Exception:
            if terminal_error is not None:
                raise terminal_error
            if blocker is not None:
                raise blocker
            raise
        complete()
        state.experiment_feedback = None
        state.experiment_expansion_requested = False
        return NodeResult("refine-logs/EXPERIMENT_RESULTS.md", {
            "phase": "experiment_bridge", "iterations": result.iterations,
            "coverage": "experiments/coverage.json", "manifest": "experiments/manifest.json",
            "completed_jobs": len(state.experiment_completed_receipts),
        })


    @artifact_contract
    def _experiment_manifest(self) -> dict[str, Any]:
        self._require_file("experiments/manifest.json")
        return self.experiments.load_manifest()

    def _run_experiment_check(self, state: WorkflowState, prompt: str) -> NodeResult:
        self.experiments.compute_config()
        manifest = self._experiment_manifest()
        missing = [p for p in manifest["required_inputs"] if not self.experiments.input_path(p).exists()]
        if missing:
            raise AwaitingResults(f"Missing experiment inputs: {missing}. Supply real data and resume.")
        receipt = self.experiments.execute(manifest["smoke"])
        return NodeResult(receipt["log"], {"phase": "experiment_check", "smoke": receipt,
                                          "manifest": manifest})

    def _checked_manifest(self, state: WorkflowState) -> dict[str, Any]:
        manifest = self._experiment_manifest()
        if manifest != state.node_results.get("experiment_check", {}).get("manifest"):
            raise RuntimeError("manifest not smoke-checked; call check_experiments again")
        return manifest

    def _run_experiment_run(self, state: WorkflowState, prompt: str) -> NodeResult:
        manifest = self._checked_manifest(state)
        receipts = run_queue(self.experiments, state, manifest, on_progress=self.on_node_progress)
        return NodeResult("experiment-runs", {"phase": "experiment_run", "jobs": receipts})

    @staticmethod
    def _require_no_deferred_jobs(manifest: dict[str, Any]) -> None:
        deferred = manifest.get("deferred_jobs", [])
        if deferred:
            detail = "; ".join(f"{item['id']}: {item['reason']}" for item in deferred)
            raise AwaitingResults(
                "Available experiment jobs completed; deferred tasks prevent paper generation. "
                "Review results/1_dataset_analysis/ and resolve inputs/design before re-planning. " + detail)

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

    def _inspect_experiment_plan(self) -> str:
        path = self._require_file("refine-logs/EXPERIMENT_PLAN.md")
        text = path.read_text(encoding="utf-8")
        headings = [line.strip() for line in text.splitlines()
                    if line.lstrip().startswith("#")]
        return json.dumps({"path": "refine-logs/EXPERIMENT_PLAN.md",
                           "bytes": path.stat().st_size,
                           "headings": headings,
                           "content": text[:12000]}, ensure_ascii=False)


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
            f"Original task:\n{prompt}\n\nCreate the paper plan. "
            f"Available measured result files: {paths}. "
            "Read refine-logs/EXPERIMENT_RESULTS.md and experiments/coverage.json when present. "
            "Preserve a mapping for every original figure/panel evidence task; move or merge "
            "only with an explicit destination, and never silently remove required evidence.", phase="paper_plan")
        self._require_file("PAPER_PLAN.md")
        return NodeResult("PAPER_PLAN.md", {"phase": "paper_plan", "results": paths, "iterations": result.iterations})

    def _run_paper_figure(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        if not self._result_files():
            raise AwaitingResults("Experiment results are missing from results/.")
        image_instruction = (
            " Figure 1 must be generated with generate_image at "
            "figures/figure_1_motivation_architecture.png. Design its prompt from the actual "
            "method and PAPER_PLAN.md, with panels (a) Motivation and (b) Architecture. "
            "Do not invent measurements or claims. Include the generated image in "
            "latex_includes.tex and update captions before finishing. A successful "
            "generate_image tool receipt from this invocation is required; existing files alone "
            "do not satisfy this requirement."
            if self.require_image_figure else ""
        )
        result = self._go(
            f"Original task:\n{prompt}\n\nProduce the planned figures from PAPER_PLAN.md "
            "and results/ using the phase skill. You own visual inspection and repair: "
            "view every final figure with view_image, fix layout problems and view again after edits. "
            "Write figures/visual_checks.json with final asset/preview hashes and specific findings "
            "as described in the skill. Completion checks reject missing or stale visual reviews; "
            "do not leave this work to an external supervisor." + image_instruction, phase="paper_figure")
        self._require_file("figures/latex_includes.tex")
        self._require_file("figures/figure_plan.md")
        self._require_file("figures/figure_captions.md")
        metadata: dict[str, Any] = {"phase": "paper_figure", "iterations": result.iterations}
        images = self._figure_receipts(getattr(result, "messages", []))
        if images:
            metadata.update(image_backend="project-mcp", images=list(images.values()))
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
            images[str(image_path.relative_to(self._project))] = job
        for relative, job in images.items():
            data = self._require_file(relative).read_bytes()
            if len(data) < 100 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ArtifactValidationError(f"Image receipt output is not a PNG: {relative}")
            if hashlib.sha256(data).hexdigest() != job.get("sha256"):
                raise ArtifactValidationError(f"Image changed after generate_image: {relative}")
        expected = "figures/figure_1_motivation_architecture.png"
        if self.require_image_figure and expected not in images:
            raise ArtifactValidationError("Figure 1 requires a successful generate_image tool receipt from this invocation")
        return images

    def _run_paper_write(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        self._require_file("figures/latex_includes.tex")
        result = self._go(
            f"{prompt}\nDraft the paper from PAPER_PLAN.md and verified results/. Use generated figures "
            "and verified citations only. Write paper/main.tex, paper/sections/*.tex, and "
            "paper/references.bib. Preserve an existing paper where practical.", phase="paper_write")
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
            cwd=source.parent, capture_output=True, text=True, timeout=180,
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
        try:
            pdf = self._compile_pdf()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            def check():
                try:
                    self._compile_pdf()
                except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                    raise ArtifactValidationError(str(error)) from error

            result = self._go(
                f"{prompt}\nFix the LaTeX build in paper/. Read the build log, repair the source "
                f"or install missing dependencies, and verify compilation. Error:\n{exc}",
                phase="paper_compile", completion_check=check)
            pdf = self._require_file("paper/main.pdf")
            return NodeResult(str(pdf.relative_to(self._project)),
                              {"phase": "paper_compile", "iterations": result.iterations})
        return NodeResult(str(pdf.relative_to(self._project)), {"phase": "paper_compile"})

    def _next_node(self, state: WorkflowState, node: str) -> str | None:
        if node == "literature":
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
            return None
        return None

    @staticmethod
    def _fail(state: WorkflowState, message: str) -> WorkflowState:
        state.status = "failed"
        state.error = message
        return state


__all__ = ["WorkflowConfig", "WorkflowRunner", "WorkflowState"]
