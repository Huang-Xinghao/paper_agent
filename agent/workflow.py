"""Deterministic workflow controller for research and paper production."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import re
import csv
import shutil
import subprocess
import os
import math
import signal
import sys
import uuid
import time
from functools import wraps
from paper_agent.project_config import load_compute_config, compute_context, validate_job_timeout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .loop import ArtifactValidationError, PhaseAgent
from paper_agent.tools.review import CodexReviewer, external_review
from paper_agent.tools import ToolContext
from paper_agent.tools.battery_resources import search_battery_resources, read_battery_resource
from paper_agent.tools.literature import search_papers, discover_local_papers, read_local_paper, download_arxiv_paper
from paper_agent.tools.wiki import search_wiki, read_wiki

WorkflowStatus = Literal["running", "awaiting_results", "completed", "rejected", "review_failed", "failed"]
PaperStatus = Literal["pending", "pdf_ready", "review_completed", "review_failed"]


class AwaitingResults(Exception):
    """The experiment code is ready, but real measurements are not yet present."""


class ExperimentExecutionError(RuntimeError):
    """An executed job failed; its receipt and log are already durable."""


class ExperimentCodeRejected(RuntimeError):
    """A completed independent review found blocking implementation defects."""


class ExperimentExpansionRequired(Exception):
    """Data preparation completed and the bridge must expand deferred jobs."""


def artifact_contract(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ArtifactValidationError(str(exc)) from exc
    return checked


@dataclass
class ReviewResult:
    """Normalized result from an independent reviewer."""

    score: float | None = None
    verdict: str | None = None
    confidence: float | None = None
    reviewer_model: str | None = None
    raw_response: str = ""
    parse_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_proceed(self) -> bool:
        return (self.verdict or "").upper().replace(" ", "_") in {
            "PROCEED", "PROCEED_WITH_CAUTION", "READY", "ACCEPT_AS_IDEA",
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowState:
    """Serializable state passed between workflow nodes."""

    run_id: str
    task_prompt: str = ""
    current_node: str = "ideas"
    status: WorkflowStatus = "running"
    paper_status: PaperStatus = "pending"
    artifacts: dict[str, str] = field(default_factory=dict)
    review: ReviewResult | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    transitions: int = 0  # Lifetime successful node executions; never a run budget.
    revision_count: int = 0
    fault_retry_count: int = 0
    failed_node: str | None = None
    error: str | None = None
    node_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    idea_candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_idea: dict[str, Any] | None = None
    experiment_repair_count: int = 0
    experiment_feedback: dict[str, Any] | None = None
    experiment_review_status: str = "pending"
    last_experiment_failure: dict[str, Any] | None = None
    experiment_expansion_requested: bool = False
    experiment_expansion_count: int = 0
    experiment_completed_receipts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.review is not None:
            value["review"] = self.review.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        fields = {name: value[name] for name in cls.__dataclass_fields__
                  if name in value and name != "review"}
        review = value.get("review")
        fields["review"] = ReviewResult(**review) if isinstance(review, dict) else None
        if "failed_node" not in value and value.get("status") == "failed":
            node = value.get("current_node")
            if str(value.get("error", "")).startswith(f"node {node} failed:"):
                fields["failed_node"] = node
        return cls(**fields)


@dataclass(frozen=True)
class WorkflowConfig:
    """Routing and retry limits for the research-to-paper workflow."""

    start_node: str = "ideas"
    min_proceed_score: float = 7.0
    min_revision_score: float = 5.0
    max_revisions: int = 1
    max_node_steps_per_run: int | None = None
    max_paper_review_rounds: int = 2
    max_artifact_repairs: int = 2
    max_experiment_repairs: int = 3
    arxiv_download: bool = True
    arxiv_max_download: int = 5
    arxiv_read_pages: int = 3


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
                 reviewer: Any | None = None,
                 code_reviewer: Any | None = None,
                 on_node_start: Callable[[str, WorkflowState], None] | None = None,
                 on_node_end: Callable[[str, WorkflowState, NodeResult], None] | None = None,
                 on_node_progress: Callable[[str, WorkflowState, dict[str, Any]], None] | None = None):
        self.agent = agent
        # Retain legacy review integrations for compatibility; no active node invokes them.
        self.reviewer = reviewer
        self.code_reviewer = code_reviewer
        # Production phase-agent runs begin with the explicit literature phase;
        # lightweight doubles retain the historical ideas entry point.
        self.config = config or WorkflowConfig(
            start_node="literature" if isinstance(agent, PhaseAgent) else "ideas")
        self.idea_config = idea_config or IdeaNodeConfig()
        if not math.isfinite(self.idea_config.max_total_gpu_hours) or self.idea_config.max_total_gpu_hours <= 0:
            raise ValueError("max_total_gpu_hours must be a finite positive number")
        if type(self.idea_config.search_paper_number) is not int or not 1 <= self.idea_config.search_paper_number <= 50:
            raise ValueError("search_paper_number must be between 1 and 50")
        if self.config.max_revisions < 0:
            raise ValueError("max_revisions must be nonnegative")
        if self.config.max_node_steps_per_run is not None and self.config.max_node_steps_per_run < 1:
            raise ValueError("max_node_steps_per_run must be positive")
        if self.config.max_paper_review_rounds < 1:
            raise ValueError("max_paper_review_rounds must be at least 1")
        if not 1 <= self.config.arxiv_max_download <= 50:
            raise ValueError("arxiv_max_download must be between 1 and 50")
        if not 1 <= self.config.arxiv_read_pages <= 10:
            raise ValueError("arxiv_read_pages must be between 1 and 10")
        for name in ("max_artifact_repairs", "max_experiment_repairs"):
            value = getattr(self.config, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        self.run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
        self.on_node_start = on_node_start
        self.on_node_end = on_node_end
        self.on_node_progress = on_node_progress
        self._ideas_research: dict[str, Any] | None = None
        self._ideas_research_key: tuple[str, str, int] | None = None
        self.require_image_figure = os.environ.get(
            "PAPER_AGENT_ENABLE_IMAGE_FIGURE", "").lower() in {"1", "true", "yes"}
        self._nodes: dict[str, Callable[[WorkflowState, str], NodeResult]] = {
            "literature": self._run_literature,
            "ideas": self._run_ideas,
            "novelty": self._run_novelty,
            "refine": self._run_refine,
            "experiment_plan": self._run_experiment_plan,
            "experiment_bridge": self._run_experiment_bridge,
            "experiment_check": self._run_experiment_check,
            "experiment_run": self._run_experiment_run,
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
        # Resume old checkpoints through the main-agent path without review gates.
        removed_nodes = {"review": "refine", "experiment_code_review": "experiment_check",
                         "auto_paper_improvement_loop": "paper_compile"}
        if state.current_node in removed_nodes:
            state.current_node = removed_nodes[state.current_node]
            if state.status in {"review_failed", "rejected"}:
                state.status = "running"
                state.error = None
        if state.status == "awaiting_results":
            state.status = "running"
            state.error = None
        if state.status != "running":
            return state

        # Guard accidental routing loops within this invocation only. Scientific
        # revisions have their own limit; prior successful runs cost no budget.
        steps_this_run = 0
        step_limit = self.config.max_node_steps_per_run or (
            len(self._nodes) * (self.config.max_revisions + 1) + 4 * self.config.max_experiment_repairs)
        while state.status == "running":
            if (state.experiment_feedback and state.current_node in {
                    "experiment_check", "experiment_run"}):
                state.current_node = "experiment_bridge"
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
                result = self._run_with_repairs(node, handler, state, prompt + "\n\n" + compute_context(self._project))
            except AwaitingResults as exc:
                state.status = "awaiting_results"
                state.error = str(exc)
                return state
            except ExperimentExpansionRequired as exc:
                state.experiment_expansion_requested = True
                state.current_node = "experiment_bridge"
                state.error = str(exc)
                continue
            except (ExperimentExecutionError, ExperimentCodeRejected) as exc:
                try:
                    self._queue_experiment_repair(state, node, exc)
                except Exception as repair_error:
                    state.failed_node = node
                    if isinstance(exc, ExperimentCodeRejected):
                        state.status = "rejected"
                        state.error = str(repair_error)
                        return state
                    return self._fail(state, str(repair_error))
                continue
            except Exception as exc:
                state.failed_node = node
                if node == "experiment_code_review":
                    state.experiment_review_status = "incomplete"
                    state.status = "review_failed"
                    state.error = f"node {node} failed: {exc}"
                    return state
                if node == "auto_paper_improvement_loop" and state.paper_status == "pdf_ready":
                    state.paper_status = "review_failed"
                    state.status = "review_failed"
                    state.error = f"node {node} failed: {exc}"
                    return state
                return self._fail(state, f"node {node} failed: {exc}")
            state.artifacts[node] = result.artifact
            state.node_results[node] = {
                "artifact": result.artifact,
                **result.metadata,
            }
            # PhaseAgent is its own runtime.  Keep the ``app`` fallback for
            # lightweight test doubles and older integrations.
            messages = getattr(self.agent, "last_state", None)
            if messages is None:
                messages = getattr(getattr(self.agent, "app", None), "last_state", None)
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

    def _repair_snapshot(self) -> dict[str, str]:
        # Code, plans and configs only; logs/results must not count as progress.
        excluded = {"results", "smoke-results", "experiment-runs", ".paper-agent",
                    "research-wiki", ".git", ".venv", "__pycache__", "data"}
        return {str(p.relative_to(self._project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self._project.rglob("*")
                if not any(part in excluded for part in p.relative_to(self._project).parts)
                and p.is_file() and not p.is_symlink()
                and p.suffix in {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".tex"}
                and not p.name.startswith("workflow-")}

    def _record_repair(self, state: WorkflowState, node: str, kind: str,
                       attempt: int, error: Exception) -> None:
        entry = {"node": node, "kind": kind, "attempt": attempt, "error": str(error),
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        state.history.append(entry)
        state.fault_retry_count += 1
        directory = self._project / ".paper-agent" / "repairs"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{uuid.uuid4().hex}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.on_node_start:
            self.on_node_start(node, state)  # checkpoint repair intent before calling model

    def _run_with_repairs(self, node, handler, state, prompt):
        artifact_attempts = 0
        previous_failure = None
        feedback = ""
        repairable = {"novelty", "refine", "experiment_plan", "experiment_bridge",
                      "paper_plan", "paper_figure", "paper_write"}
        while True:
            try:
                return handler(state, prompt + feedback)
            except ArtifactValidationError as exc:
                if node not in repairable or artifact_attempts >= self.config.max_artifact_repairs:
                    raise
                fingerprint = (str(exc), self._repair_snapshot())
                if fingerprint == previous_failure:
                    raise RuntimeError(f"Artifact repair made no progress: {exc}") from exc
                previous_failure = fingerprint
                artifact_attempts += 1
                self._record_repair(state, node, "artifact_repair", artifact_attempts, exc)
                feedback = (
                    f"\n\nRunner validation failed (repair {artifact_attempts}/"
                    f"{self.config.max_artifact_repairs}): {exc}\n"
                    "Correct the generated artifact using the existing evidence. Preserve the task, "
                    "scientific requirements, source datasets, measurements and user resource settings. "
                    "Do not weaken checks or invent missing evidence. The runner will validate again."
                )

    def _experiment_evidence_snapshot(self):
        paths = set()
        for pattern in ("results/**/*", "smoke-results/**/*", "experiment-runs/**/*",
                        "project-config.json", "experiments/resources.json"):
            paths.update(p for p in self._project.glob(pattern) if p.is_file())
        return {str(p.relative_to(self._project)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths}

    def _queue_experiment_repair(self, state, node, error):
        """Persist feedback and route to bridge; never call an agent or run a job here."""
        if node not in {"experiment_code_review", "experiment_check", "experiment_run"}:
            raise error
        if state.experiment_repair_count >= self.config.max_experiment_repairs:
            raise RuntimeError(f"Experiment repair limit reached: {error}") from error
        fingerprint = {"error": str(error).split("; log:")[0], "code": self._repair_snapshot()}
        if isinstance(error, ExperimentExecutionError) and fingerprint == state.last_experiment_failure:
            raise RuntimeError(f"Experiment repair made no progress: {error}") from error
        state.last_experiment_failure = fingerprint
        state.experiment_repair_count += 1
        state.experiment_feedback = {
            "source_node": node, "error": str(error), "attempt": state.experiment_repair_count,
            "protected_evidence": self._experiment_evidence_snapshot(),
            "manifest": self._experiment_manifest() if isinstance(error, ExperimentExecutionError) else None,
        }
        for stale in ("experiment_code_review", "experiment_check", "experiment_run"):
            state.node_results.pop(stale, None)
            state.artifacts.pop(stale, None)
        state.experiment_review_status = "pending"
        state.current_node = "experiment_bridge"
        self._record_repair(state, node, "experiment_repair", state.experiment_repair_count, error)

    def start_from_project(self, node: str, prompt: str,
                           state: WorkflowState | None = None) -> WorkflowState:
        """Prepare an explicit entry point from durable outputs, checking its inputs."""
        if node not in self._nodes:
            raise ValueError(f"unknown workflow node: {node}")
        if state is None:
            state = WorkflowState(self.run_id, task_prompt=prompt)
        if state.task_prompt and state.task_prompt != prompt:
            raise ValueError("saved workflow task differs from the supplied prompt")
        required = {
            "ideas": ["literature/RESEARCH_REPORT.md"],
            "novelty": ["idea-stage/IDEA_REPORT.md"],
            "refine": ["idea-stage/IDEA_REPORT.md", "idea-stage/NOVELTY.json"],
            "experiment_plan": ["refine-logs/FINAL_PROPOSAL.md"],
            "experiment_bridge": ["refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
                                   "refine-logs/EXPERIMENT_TRACKER.md"],
            "experiment_check": ["experiments/manifest.json"],
            "paper_plan": ["refine-logs/FINAL_PROPOSAL.md",
                           "refine-logs/EXPERIMENT_PLAN.md"],
            "paper_figure": ["PAPER_PLAN.md"],
            "paper_write": ["PAPER_PLAN.md", "figures/latex_includes.tex"],
            "paper_compile": ["paper/main.tex"],
        }
        for relative in required.get(node, []):
            self._require_file(relative)
        if node == "experiment_run":
            if "experiment_check" not in state.node_results:
                raise ValueError("experiment_run requires a saved experiment_check result; start at experiment_check")
            smoke = state.node_results["experiment_check"].get("smoke", {})
            if smoke.get("status") != "completed":
                raise ValueError("experiment_run requires a completed smoke-test receipt")
        if node in {"novelty", "refine"}:
            state.artifacts["ideas"] = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if node == "refine":
            state.artifacts["novelty"] = self._require_file("idea-stage/NOVELTY.json").read_text(encoding="utf-8")
        state.task_prompt = prompt
        state.current_node = node
        state.status = "running"
        state.error = None
        return state

    def _research_context(self, query: str, *, stage: str, max_results: int = 5) -> dict[str, Any]:
        """Read bounded source material before an idea or experiment-plan phase."""
        if not isinstance(self.agent, PhaseAgent):
            return {}
        project = self._project
        context: dict[str, Any] = {"wiki": {"status": "unavailable"}}
        tool_ctx = ToolContext(project)
        wiki_root = project / "research-wiki"
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
                    relative = path.relative_to("research-wiki").as_posix()
                    if relative not in seen and path.parent.name in {"papers", "ideas", "experiments", "claims"}:
                        seen.add(relative)
                        pages.append(relative)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"search: {exc}")
            records = []
            for relative in pages[:7]:
                try:
                    result = read_wiki(tool_ctx, path=f"research-wiki/{relative}", limit=220)
                    if result.is_error:
                        raise ValueError(result.output)
                    payload = json.loads(result.output)
                    records.append({"path": payload["path"], "content": payload["content"][:6000],
                                    "truncated": payload["next_offset"] is not None or len(payload["content"]) > 6000})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(f"{relative}: {exc}")
            context["wiki"] = {"status": "partial" if errors and records else
                               "read" if records else "read_failed" if errors else "empty",
                               "pages": records, "errors": errors}

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
        """Perform the baseline PDF screening before asking the model to synthesize."""
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
        arxiv: dict[str, Any] = {"status": "disabled", "papers": [], "errors": []}
        if self.config.arxiv_download:
            records, errors = [], []
            candidates = []
            try:
                search = payload(search_papers(ctx, query=query[:500], source="arxiv",
                                              max_results=self.config.arxiv_max_download))
                candidates = search["papers"][:self.config.arxiv_max_download]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"search: {exc}")
            seen = set()
            for paper in candidates:
                record = dict(paper)
                try:
                    download = payload(download_arxiv_paper(ctx, url=paper["url"]))
                    if download["arxiv_id"] in seen:
                        continue
                    seen.add(download["arxiv_id"])
                    record["download"] = download
                    record["reading"] = screen(download["path"], self.config.arxiv_read_pages)
                    record["status"] = "read"
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    record["status"] = "read_failed" if "download" in record else "download_failed"
                    record["error"] = str(exc)
                    errors.append(f"{paper.get('url')}: {exc}")
                records.append(record)
            successful = sum(record.get("status") == "read" for record in records)
            arxiv = {"status": "partial" if errors and successful else "read" if successful else
                     "failed" if errors else "empty", "papers": records, "errors": errors,
                     "selection": "arxiv API relevance order", "max_download": self.config.arxiv_max_download,
                     "read_pages": self.config.arxiv_read_pages}
        return {"local_papers": local, "arxiv_papers": arxiv}

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
        research_key = (state.run_id, prompt, search_paper_number)
        if self._ideas_research is None or self._ideas_research_key != research_key:
            self._ideas_research = self._research_context(
                prompt, stage="ideas", max_results=search_paper_number)
            self._ideas_research_key = research_key
        research = self._ideas_research
        instruction = (
            "Generate a concrete, falsifiable research idea and preserve it as a project artifact. "
            "Write a complete standalone report; do not describe the generation or revision process."
        )
        result = self.agent.go(
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
        # The canonical file is the hand-off path used by later nodes. Keep an
        # immutable, numbered copy as well so each generated idea remains
        # independently reviewable and can be restored after ranking.
        version_path = self._save_idea_version(state.revision_count, idea)
        messages = getattr(result, "messages", ())
        successful = {message.get("name") for message in messages
                      if message.get("role") == "tool" and not message.get("is_error")}
        if "search_papers" in successful and "verify_papers" not in successful:
            raise ValueError("ideas used search_papers without a successful verify_papers call")
        return NodeResult(idea, {"phase": "ideas", "iterations": result.iterations,
                                        "revision": state.revision_count,
                                        "version_path": str(version_path.relative_to(self._project)),
                                        "canonical_path": "idea-stage/IDEA_REPORT.md",
                                        "literature_source": literature_path,
                                        "literature_sha256": hashlib.sha256(literature.encode("utf-8")).hexdigest(),
                                        "idea_parameters": {
                                            "max_total_gpu_hours": max_total_gpu_hours,
                                            "search_paper_number": search_paper_number},
                                        "research_sources": self._research_sources(research)})

    def _save_idea_version(self, revision: int, idea: str) -> Path:
        """Persist a numbered idea snapshot while retaining the canonical hand-off."""
        versions = self._project / "idea-stage" / "revisions"
        versions.mkdir(parents=True, exist_ok=True)
        target = versions / f"IDEA_REPORT.v{revision}.md"
        target.write_text(idea, encoding="utf-8")
        return target

    @staticmethod
    def _research_sources(research: dict[str, Any]) -> dict[str, Any]:
        return {
            "wiki": [page["path"] for page in research.get("wiki", {}).get("pages", [])],
            "online_papers": [paper.get("doi") or paper.get("url") or paper.get("title")
                              for paper in research.get("online_papers", {}).get("papers", [])],
            "battery_resources": [item["resource_id"] for item in
                                  research.get("battery_resources", {}).get("resources", [])],
            **({"local_papers": [paper["path"] for paper in research["local_papers"]["papers"]],
                "arxiv_papers": [paper["download"]["path"] for paper in research["arxiv_papers"]["papers"]
                                 if "download" in paper]} if "local_papers" in research else {}),
            "status": {name: value.get("status") for name, value in research.items()},
        }

    def _run_literature(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Collect bounded literature context; the phase agent owns interpretation."""
        research = self._research_context(prompt, stage="literature")
        result = self.agent.go(
            "Research task:\n" + prompt + "\nRunner-collected literature context:\n" +
            json.dumps(research, ensure_ascii=False), phase="literature"
        )
        target = self._project / "literature/RESEARCH_REPORT.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        # The literature skill may write the report through the `write` tool
        # before returning a short completion message. Preserve that artifact;
        # only use the final model text when this run did not write the target.
        target_written = any(
            message.get("role") == "tool"
            and message.get("name") in {"write", "edit"}
            and not message.get("is_error")
            and "literature/RESEARCH_REPORT.md" in str(message.get("content", ""))
            for message in getattr(result, "messages", ())
        )
        if target_written and target.is_file():
            report = target.read_text(encoding="utf-8")
        else:
            report = result.final
            target.write_text(report, encoding="utf-8")
        return NodeResult(report, {"phase": "literature", "iterations": result.iterations,
                                   "research_sources": self._research_sources(research)})

    def _run_review(self, state: WorkflowState, prompt: str) -> NodeResult:
        idea = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        novelty = self._require_file("idea-stage/NOVELTY.json").read_text(encoding="utf-8")
        if not idea.strip():
            raise ValueError("idea-stage/IDEA_REPORT.md is empty")
        self._validate_novelty_json(json.loads(novelty))
        state.artifacts["ideas"] = idea
        state.artifacts["novelty"] = novelty
        review_boundary = (
            "Review the idea, its internal logic, novelty evidence, and validation plan. "
            "The idea phase does not fully audit real datasets; lack of completed data auditing "
            "or experiment results alone is not a BLOCKER or required idea revision. "
            "Record unverified data conditions as pre-experiment checks. Do not include strengths.\n\n"
        )
        # Compatibility hook for lightweight test/embedding agents. Production
        # PhaseAgent instances always use the normal review phase below.
        if self.reviewer is not None and not isinstance(self.agent, PhaseAgent):
            review_prompt = (
                review_boundary + "You are an independent scientific idea reviewer. Return ONLY valid JSON with score, verdict, confidence, summary, "
                "critical_issues, minimum_next_steps, and novelty_assessment.\n\n"
                f"Original task:\n{prompt}\n\nCurrent idea artifact:\n{idea}"
                f"\n\nNovelty assessment:\n{novelty}"
            )
            try:
                payload = self.reviewer.review(self.agent.project, review_prompt)
                content = payload.get("content", "")
                if not isinstance(content, str):
                    raise RuntimeError("reviewer returned no text content")
                review = parse_review_result(type("ReviewResponse", (), {
                    "final": content, "messages": []
                })())
                return self._record_idea_review(state, review, content, "custom-reviewer",
                                                thread_id=payload.get("threadId"))
            except Exception as exc:
                fallback = self.agent.go(
                    review_boundary + f"Return ONLY valid JSON with score, verdict, confidence, summary, "
                    f"critical_issues, minimum_next_steps, and novelty_assessment.\n\n"
                    f"Original task:\n{prompt}\n\nCurrent idea artifact:\n{idea}"
                    f"\n\nNovelty assessment:\n{novelty}",
                    phase="review")
                review = parse_review_result(fallback)
                return self._record_idea_review(state, review, fallback.final,
                                                "local-fallback", fallback_reason=str(exc))
        result = self.agent.go(
            review_boundary + f"Original task:\n{prompt}\n\nCurrent idea artifact:\n{idea}\n\n"
            f"Novelty assessment:\n{novelty}",
            phase="review",
        )
        review = parse_review_result(result)
        return self._record_idea_review(state, review, result.final, "agent-review")

    def _record_idea_review(self, state: WorkflowState, review: ReviewResult,
                            content: str, provider: str, **metadata: Any) -> NodeResult:
        state.review = review
        idea = self._require_file("idea-stage/IDEA_REPORT.md").read_text(encoding="utf-8")
        if review.score is not None and not review.parse_error:
            version_path = self._project / "idea-stage" / "revisions" / \
                f"IDEA_REPORT.v{state.revision_count}.md"
            state.idea_candidates.append({"revision": state.revision_count,
                                          "idea": idea, "review": review.to_dict(),
                                          "version_path": str(version_path.relative_to(self._project))
                                          if version_path.is_file() else None,
                                          "provider": provider, **metadata})
        # The review skill's durable contract is established at the review
        # node itself. Later nodes may update selected_idea/candidates, but
        # Starting at refine must already have a valid REVIEW.json.
        review_path = self._project / "idea-stage/REVIEW.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(
            json.dumps({"review": review.to_dict(),
                        "selected_idea": None,
                        "candidates": state.idea_candidates},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        readable = self._format_review_markdown(review, content)
        (self._project / "idea-stage/REVIEW.md").write_text(readable, encoding="utf-8")
        if review.parse_error or review.score is None:
            raise ValueError(f"invalid idea review: {review.parse_error or 'missing score'}")
        return NodeResult(content, {"phase": "review", "review": review.to_dict(),
                                    "provider": provider, **metadata})

    @staticmethod
    def _format_review_markdown(review: ReviewResult, content: str) -> str:
        """Persist the review as a researcher-readable improvement memo."""
        payload = review.details
        lines = ["# 研究方案评审与改进意见", "",
                 f"- 评分：{review.score if review.score is not None else '未知'} / 10",
                 f"- 结论：{review.verdict or '未知'}",
                 f"- 评审模型：{review.reviewer_model or 'unknown'}", ""]
        if payload:
            sections = (("总评", "summary"),
                        ("维度评分", "dimension_scores"),
                        ("关键问题", "critical_issues"),
                        ("必须修改", "required_changes"),
                        ("必需修改与最小验证", "minimum_next_steps"),
                        ("新颖性判断", "novelty_assessment"),
                        ("证据缺口", "evidence_gaps"))
            for title, key in sections:
                value = payload.get(key)
                if not value and key not in {"dimension_scores", "required_changes"}:
                    continue
                lines.extend([f"## {title}", ""])
                if not value and key == "dimension_scores":
                    lines.extend(["评审未提供三个独立维度评分。", ""])
                    continue
                if not value and key == "required_changes":
                    lines.extend(["评审未提供必需修改清单。", ""])
                    continue
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            severity = item.get("severity", "")
                            issue = item.get("issue", "")
                            evidence = item.get("evidence", "")
                            change = item.get("required_change", "")
                            test = item.get("resolution_test", "")
                            text = f"**{severity}** {issue}" if severity else issue
                            if evidence:
                                text += f"\n证据：{evidence}"
                            if change:
                                text += f"\n必需修改：{change}"
                            if test:
                                text += f"\n最小验证：{test}"
                            lines.extend([f"- {text}", ""])
                        else:
                            lines.extend([f"- {item}", ""])
                elif isinstance(value, dict):
                    labels = {
                        "problem_importance": "解决问题的重要性",
                        "method_novelty": "使用方法的新颖性",
                        "method_soundness": "所提出方法的合理性",
                    }
                    for dimension, result in value.items():
                        if isinstance(result, dict):
                            score = result.get("score", "未知")
                            assessment = result.get("assessment", "")
                            text = f"**{labels.get(dimension, dimension)}：{score} / 10**"
                            if assessment:
                                text += f"\n{assessment}"
                            lines.extend([f"- {text}", ""])
                        else:
                            lines.extend([f"- **{labels.get(dimension, dimension)}**：{result}", ""])
                else:
                    lines.extend([str(value), ""])
            return "\n".join(lines).rstrip() + "\n"
        lines.extend(["## 批判性与建设性意见", "", content.strip(), ""])
        return "\n".join(lines)

    def _select_best_idea(self, state: WorkflowState) -> None:
        if not state.idea_candidates:
            raise ValueError("no idea has a valid scored review")
        best = max(state.idea_candidates, key=lambda item: item["review"]["score"])
        idea = best["idea"]
        (self._project / "idea-stage/IDEA_REPORT.md").write_text(idea, encoding="utf-8")
        state.artifacts["ideas"] = idea
        state.review = ReviewResult(**best["review"])
        state.selected_idea = {"revision": best["revision"],
                               "score": state.review.score, "provider": best["provider"],
                               "version_path": best.get("version_path")}
        (self._project / "idea-stage/REVIEW.json").write_text(
            json.dumps({"review": state.review.to_dict(), "selected_idea": state.selected_idea,
                        "candidates": state.idea_candidates}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    @staticmethod
    def _normalize_novelty_json(payload: Any) -> Any:
        """Accept the legacy idea label, but emit only the current field name."""
        if not isinstance(payload, dict):
            return payload
        normalized = dict(payload)
        if "idea_under_review" in normalized:
            normalized.setdefault("idea_under_check", normalized.pop("idea_under_review"))
        return normalized

    @staticmethod
    @artifact_contract
    def _validate_novelty_json(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("NOVELTY.json must be an object")
        payload = WorkflowRunner._normalize_novelty_json(payload)
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
        payload = WorkflowRunner._normalize_novelty_json(payload)
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
        result = self.agent.go(
            f"{prompt}\n\nAssess the approved idea for novelty and identify its strongest competing prior work. "
            "Write ONLY a JSON object to idea-stage/NOVELTY.json using the schema in the novelty-check skill. "
            "Your final message is only a completion notice."
            f"\n\nIdea:\n{state.artifacts.get('ideas', '')}",
            phase="novelty",
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
        payload = self._normalize_novelty_json(payload)
        self._validate_novelty_json(payload)
        raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target.write_text(raw, encoding="utf-8")
        (self._project / "idea-stage/NOVELTY.md").write_text(self._render_novelty_markdown(payload), encoding="utf-8")
        return NodeResult(raw, {"phase": "novelty", "iterations": result.iterations})

    def _run_refine(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Turn the approved idea and its assessments into the final proposal."""
        input_paths = ["idea-stage/IDEA_REPORT.md"]
        input_snapshot = self._inspect_project_artifacts(input_paths)
        research = self._research_context(
            f"{prompt} {state.artifacts.get('ideas', '')[:500]}", stage="research_refine")
        result = self.agent.go(
            f"""{prompt}

Refine the generated idea using the inputs below.
Runner-verified input snapshot:
{json.dumps(input_snapshot, ensure_ascii=False)}

Idea artifact:
{state.artifacts.get('ideas', '')}

Novelty assessment:
{state.artifacts.get('novelty', '')}

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
        result = self.agent.go(
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
        """Implement concrete, reproducible experiment assets from the plan."""
        expansion = state.experiment_expansion_requested
        for stale in ("experiment_code_review", "experiment_check", "experiment_run"):
            if expansion and stale == "experiment_run":
                continue
            state.node_results.pop(stale, None)
            state.artifacts.pop(stale, None)
        state.experiment_review_status = "pending"
        feedback = state.experiment_feedback
        if expansion:
            prompt += (
                "\nThe executable data-preparation jobs have completed successfully. "
                "Now inspect their durable results and automatically expand the deferred "
                "experiment matrix into concrete manifest jobs with explicit seeds, dependencies "
                "and output paths. Remove only deferred entries whose gate is resolved; keep any "
                "still genuinely blocked with a precise reason. Do not require manual re-entry.\n"
            )
        if feedback:
            prompt += (
                f"\nRepair feedback from {feedback['source_node']}, attempt {feedback['attempt']}/"
                f"{self.config.max_experiment_repairs}:\n{feedback['error']}\n"
                "Read the referenced output.log/status.json and fix the implementation. "
                "Record each finding's resolution in experiments/README.md. Preserve experiment "
                "design, source data, results, smoke-results, experiment-runs and user resource settings. "
                "Do not run experiments; workflow will smoke-test and rerun jobs. "
                + ("Preserve manifest; design/manifest changes require re-planning.\n"
                   if feedback.get("manifest") is not None else "Keep manifest aligned with the existing plan.\n"))
        self._validate_required_artifacts([
            "refine-logs/FINAL_PROPOSAL.md",
            "refine-logs/EXPERIMENT_PLAN.md",
            "refine-logs/EXPERIMENT_TRACKER.md",
        ])
        plan = self._inspect_experiment_plan()
        recovered = None
        try:
            result = self.agent.go(
                f"{prompt}\nRunner-verified experiment plan context:\n{plan}",
                phase="experiment_bridge")
        except RuntimeError as exc:
            recovered = self._recover_experiment_manifest(exc)
            if recovered is None:
                raise
            result = None
        if feedback:
            if self._experiment_evidence_snapshot() != feedback["protected_evidence"]:
                raise RuntimeError("Experiment repair modified protected evidence or resource settings")
            if feedback.get("manifest") is not None:
                try:
                    unchanged = self._experiment_manifest() == feedback["manifest"]
                except ArtifactValidationError as exc:
                    raise RuntimeError("Experiment repair changed manifest; explicit re-planning is required") from exc
                if not unchanged:
                    raise RuntimeError("Experiment repair changed manifest; explicit re-planning is required")
        self._experiment_manifest()
        package_check = self._inspect_experiment_package()
        metadata = {"phase": "experiment_bridge", "iterations": result.iterations if result else None,
                                        "inputs": [
                                            "refine-logs/FINAL_PROPOSAL.md",
                                            "refine-logs/EXPERIMENT_PLAN.md",
                                            "refine-logs/EXPERIMENT_TRACKER.md",
                                        ], "package_check": package_check}
        if recovered:
            metadata["recovery"] = recovered
        if feedback:
            metadata["repair"] = {key: feedback[key] for key in ("source_node", "error", "attempt")}
        if expansion:
            state.experiment_expansion_requested = False
            state.experiment_expansion_count += 1
            metadata["expanded_after_data_preparation"] = True
            metadata["expansion_count"] = state.experiment_expansion_count
        state.experiment_feedback = None
        return NodeResult(getattr(result, "final", "experiment package ready") if result else "experiment manifest recovered", metadata)

    def _run_experiment_code_review(self, state: WorkflowState, prompt: str) -> NodeResult:
        """Read-only independent review with durable raw responses and failure records."""
        for stale in ("experiment_check", "experiment_run"):
            state.node_results.pop(stale, None)
            state.artifacts.pop(stale, None)
        state.experiment_review_status = "pending"
        self._validate_required_artifacts([
            "refine-logs/FINAL_PROPOSAL.md", "refine-logs/EXPERIMENT_PLAN.md",
            "refine-logs/EXPERIMENT_TRACKER.md", "experiments/manifest.json",
        ])
        self._experiment_manifest()
        instructions = (Path(__file__).resolve().parents[1] / "skills" /
                        "experiment-code-review" / "SKILL.md").read_text(encoding="utf-8")
        directory = self._project / ".paper-agent" / "experiment-code-reviews" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        record = {"status": "running", "repair_count": state.experiment_repair_count,
                  "started_at": datetime.now(timezone.utc).isoformat()}
        def save():
            (directory / "review.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        save()
        relative = str((directory / "review.json").relative_to(self._project))
        state.experiment_review_status = "running"
        state.node_results["experiment_code_review"] = {"artifact": relative, "status": "running"}
        if self.on_node_progress:
            self.on_node_progress("experiment_code_review", state,
                                  {"round": state.experiment_repair_count, "stage": "review_started"})
        try:
            repair = state.node_results.get("experiment_bridge", {}).get("repair")
            brief = f"{instructions}\n\nResearch task and compute context:\n{prompt}"
            if repair:
                brief += "\n\nPrevious repair feedback (read the referenced record and verify the fix):\n" + json.dumps(repair, ensure_ascii=False)
            payload = self.code_reviewer.review(self._project, brief)
            record["response"] = payload
            save()
            raw = payload.get("content")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Code reviewer returned an empty response")
            (directory / "response.txt").write_text(raw, encoding="utf-8")
            normalized = raw.strip()
            if normalized.startswith("```"):
                normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized)
            review = json.loads(normalized)
            if (not isinstance(review, dict) or type(review.get("accepted")) is not bool
                    or not isinstance(review.get("summary"), str) or not review["summary"].strip()
                    or not isinstance(review.get("findings"), list)):
                raise ValueError("Code review requires accepted:boolean, summary:string and findings:list")
            for finding in review["findings"]:
                if (not isinstance(finding, dict) or finding.get("severity") not in {"blocker", "minor"}
                        or any(not isinstance(finding.get(key), str) or not finding[key].strip()
                               for key in ("file", "evidence", "suggestion"))
                        or type(finding.get("line")) is not int or finding["line"] < 1):
                    raise ValueError("Invalid code review finding: require severity, file, line, evidence, suggestion")
            blockers = any(item["severity"] == "blocker" for item in review["findings"])
            if review["accepted"] == blockers:
                raise ValueError("Code review accepted verdict contradicts blocking findings")
            record.update(status="completed", review=review)
        except Exception as exc:
            record.update(status="incomplete", error=str(exc))
            state.experiment_review_status = "incomplete"
            state.node_results["experiment_code_review"] = {"artifact": relative, "status": "incomplete", "error": str(exc)}
            raise
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            save()
        state.experiment_review_status = "accepted" if review["accepted"] else "changes_requested"
        metadata = {"status": state.experiment_review_status, "review": review}
        state.node_results["experiment_code_review"] = {"artifact": relative, **metadata}
        if not review["accepted"]:
            state.history.append({"node": "experiment_code_review", "artifact": relative, **metadata})
            raise ExperimentCodeRejected(f"Code review requires changes; read {relative}: {review['summary']}")
        return NodeResult(relative, metadata)

    def _recover_experiment_manifest(self, error: RuntimeError) -> dict[str, Any] | None:
        """Finish a manifest only after the experiment agent exhausts its tool budget."""
        if not str(error).startswith("model exceeded max_iterations="):
            return None
        manifest_path = self._project / "experiments/manifest.json"
        if manifest_path.is_file() and manifest_path.stat().st_size:
            self._experiment_manifest()
            return {"reason": str(error), "manifest_source": "existing"}

        generator = self._project / "experiments/build_manifest.py"
        if not generator.is_file():
            return None
        try:
            completed = subprocess.run(
                [sys.executable, str(generator)], cwd=self._project, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None
        if completed.returncode != 0:
            return None
        self._experiment_manifest()
        return {"reason": str(error), "manifest_source": "experiments/build_manifest.py"}

    def _experiment_path(self, relative: str) -> Path:
        path = (self._project / relative).resolve()
        if not path.is_relative_to(self._project):
            raise ValueError(f"experiment path escapes workspace: {relative}")
        return path

    def _experiment_input_path(self, path: str) -> Path:
        from paper_agent.tools.data_access import resolve_data_read
        root = getattr(self.agent, "knowledge_root", None)
        return resolve_data_read(self._project, (root,) if root else (), path)

    @artifact_contract
    def _experiment_manifest(self) -> dict[str, Any]:
        manifest = json.loads(self._require_file("experiments/manifest.json").read_text())
        if manifest.get("backend") != "local":
            raise ValueError("experiment manifest backend must be local")
        if manifest.get("bridge_status") == "BLOCKED":
            raise AwaitingResults(
                "Experiment bridge reported BLOCKED. Inspect experiments/README.md and "
                "experiments/configs/blocked_registry.json, resolve the documented blockers, "
                "then resume at experiment_plan or experiment_bridge; no experiments have run.")
        jobs = manifest.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("manifest requires nonempty jobs")
        seen = set()
        outputs = set()
        for job in [manifest.get("smoke", {})] + jobs:
            requirement = job.get("resources", {})
            if not isinstance(requirement, dict) or set(requirement) - {"device"} or requirement.get("device", "auto") not in {"auto", "cpu", "cuda", "mps"}:
                raise ValueError("job resources must contain only device: auto, cpu, cuda or mps")
            identifier = job.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in seen:
                raise ValueError("job IDs must be unique simple identifiers")
            seen.add(identifier)
            argv = job.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError("job argv must be a nonempty string list")
            validate_job_timeout(self._project, job)
            if str(job.get("implementation_status", "")).startswith("BLOCKED"):
                raise ValueError(f"Job {identifier} is not implemented; move it to deferred_jobs with a reason")
            expected = job.get("expected_outputs")
            if not isinstance(expected, list) or not expected:
                raise ValueError("job requires expected_outputs")
            for name in expected:
                path = self._experiment_path(name)
                prefix = "smoke-results" if job is manifest["smoke"] else "results"
                if not path.is_relative_to(self._project / prefix) or path.suffix not in {".json", ".csv"}:
                    raise ValueError(f"job outputs must be JSON/CSV under {prefix}/")
                if str(path) in outputs:
                    raise ValueError("jobs must have distinct output paths")
                outputs.add(str(path))
        deferred = manifest.get("deferred_jobs", [])
        if not isinstance(deferred, list):
            raise ValueError("deferred_jobs must be a list of non-executable task descriptions")
        for entry in deferred:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not entry["id"] or entry["id"] in seen
                    or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()
                    or "argv" in entry or "timeout_seconds" in entry):
                raise ValueError("deferred_jobs require unique IDs and reasons, without executable argv/timeout_seconds")
            seen.add(entry["id"])
        inputs = manifest.get("required_inputs")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) for p in inputs):
            raise ValueError("manifest requires required_inputs")
        return manifest

    def _resource_config(self) -> dict[str, Any]:
        return load_compute_config(self._project)

    @staticmethod
    def _available_devices() -> dict[str, Any]:
        available = {"cpu": True, "cuda_count": 0, "mps": False}
        if importlib.util.find_spec("torch") is None:
            return available
        try:
            import torch
            if torch.cuda.is_available():
                available["cuda_count"] = torch.cuda.device_count()
            available["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        except (ImportError, OSError, RuntimeError):
            pass
        return available

    def _resolve_experiment_resources(self, job: dict[str, Any]) -> dict[str, Any]:
        config = self._resource_config()
        required = job.get("resources", {}).get("device", "auto")
        selected = config["device"]
        if selected != "auto" and required != "auto" and selected != required:
            raise AwaitingResults(
                f"Experiment {job['id']} requires {required}, but project compute configuration selects {selected}. "
                "Change the resource configuration or use a matching machine, then resume.")
        available = self._available_devices()
        device = required if required != "auto" else selected
        if device == "auto":
            device = "cuda" if config["gpu_index"] is not None or available["cuda_count"] else "mps" if available["mps"] else "cpu"
        index = config["gpu_index"] if device == "cuda" else None
        if device == "cuda" and (not available["cuda_count"] or index is not None and index >= available["cuda_count"]):
            raise AwaitingResults(
                f"Experiment {job['id']} requires CUDA"
                f"{f' GPU {index}' if index is not None else ''}; this Python environment reports "
                f"{available['cuda_count']} CUDA devices. Use a matching machine or change project-config.json, then resume.")
        if device == "mps" and not available["mps"]:
            raise AwaitingResults(
                f"Experiment {job['id']} requires MPS, but it is unavailable in this Python environment. "
                "Use a matching Mac or change project-config.json, then resume.")
        return {"requested": config, "required_device": required, "allocated_device": device,
                "gpu_index": index, "cpu_threads": config["cpu_threads"], "available": available}

    def _validate_measurements(self, relative: str) -> None:
        path = self._experiment_path(relative)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"missing experiment result: {relative}")
        if path.suffix == ".json":
            value = json.loads(path.read_text())
        else:
            with path.open(newline="") as stream:
                value = list(csv.DictReader(stream))
        numbers = []
        def visit(item):
            if isinstance(item, dict):
                for child in item.values(): visit(child)
            elif isinstance(item, list):
                for child in item: visit(child)
            elif not isinstance(item, bool):
                try: numbers.append(float(item))
                except (ValueError, TypeError): pass
        visit(value)
        if not numbers or not all(math.isfinite(n) for n in numbers):
            raise RuntimeError(f"result has no finite measurements or contains NaN/Inf: {relative}")

    def _experiment_seconds_used(self) -> float:
        total = 0.0
        for path in (self._project / "experiment-runs").glob("*/status.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("status") == "running":
                raise AwaitingResults(f"Unfinished experiment receipt: {path}. Reconcile the interrupted job before resuming a budgeted run.")
            elapsed = receipt.get("elapsed_seconds")
            if elapsed is None:
                elapsed = (datetime.fromisoformat(receipt["finished"]) -
                           datetime.fromisoformat(receipt["started"])).total_seconds()
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError(f"Invalid experiment duration: {path}")
            total += elapsed
        return total

    def _execute_experiment(self, job: dict[str, Any]) -> dict[str, Any]:
        resources = self._resolve_experiment_resources(job)
        config = resources["requested"]
        timeout = job["timeout_seconds"]
        validate_job_timeout(self._project, job)
        used = self._experiment_seconds_used() if config["total_experiment_seconds"] is not None else 0.0
        if config["total_experiment_seconds"] is not None:
            remaining = config["total_experiment_seconds"] - used
            if remaining <= 0:
                raise AwaitingResults("Project total_experiment_seconds exhausted; increase the user-owned budget to continue.")
            timeout = min(timeout, remaining)
        directory = self._project / "experiment-runs" / (job["id"] + "-" + uuid.uuid4().hex[:12])
        directory.mkdir(parents=True)
        # Preserve previous outputs, so stale metrics cannot count as this attempt.
        for index, name in enumerate(job["expected_outputs"]):
            path = self._experiment_path(name)
            if path.exists():
                shutil.move(str(path), str(directory / f"previous-{index}{path.suffix}"))
        argv = list(job["argv"])
        if argv[0] in {"python", "python3"}: argv[0] = sys.executable
        env = os.environ.copy()
        env["PAPER_AGENT_DEVICE"] = resources["allocated_device"]
        backend_report = directory / "compute-backend.json"
        env["PAPER_AGENT_BACKEND_REPORT"] = str(backend_report.resolve())
        if resources["allocated_device"] == "cuda" and resources["gpu_index"] is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(resources["gpu_index"])
        elif resources["allocated_device"] != "cuda":
            env["CUDA_VISIBLE_DEVICES"] = ""
        if resources["allocated_device"] == "mps":
            env["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
        if resources["cpu_threads"] is not None:
            env["OMP_NUM_THREADS"] = str(resources["cpu_threads"])
            env["MKL_NUM_THREADS"] = str(resources["cpu_threads"])
        receipt = {"job": job, "status": "running", "argv": argv, "resources": resources,
                   "compute_backend": {"status": "unknown", "reason": "No runtime backend report"},
                   "started": datetime.now(timezone.utc).isoformat(),
                   "log": str((directory / "output.log").relative_to(self._project))}
        record = directory / "status.json"
        receipt.update(effective_timeout_seconds=timeout, prior_experiment_seconds=used)
        record.write_text(json.dumps(receipt, indent=2))
        attempt_started = time.monotonic()
        try:
            with (directory / "output.log").open("w") as log:
                process = subprocess.Popen(argv, cwd=self._project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                try:
                    code = process.wait(timeout=timeout)
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
            receipt["exit_code"] = code
            if code != 0: raise RuntimeError(f"exit code {code}")
            for name in job["expected_outputs"]: self._validate_measurements(name)
            receipt["status"] = "completed"
        except Exception as exc:
            receipt.update(status="failed", error=str(exc))
        finally:
            if backend_report.is_file():
                try:
                    backend = json.loads(backend_report.read_text(encoding="utf-8"))
                    if (not isinstance(backend, dict)
                            or backend.get("device") not in {"cpu", "cuda", "mps", "mixed"}
                            or not isinstance(backend.get("framework"), str)
                            or not backend["framework"].strip()
                            or not isinstance(backend.get("evidence"), str)
                            or not backend["evidence"].strip()):
                        raise ValueError("backend report requires device, framework and runtime evidence")
                    receipt["compute_backend"] = {
                        "status": "reported", "source": "experiment_process",
                        "report": str(backend_report.relative_to(self._project)),
                        "device": backend["device"], "framework": backend["framework"],
                        "evidence": backend["evidence"],
                    }
                except (ValueError, OSError) as exc:
                    receipt["compute_backend"] = {"status": "unknown", "reason": f"Invalid runtime backend report: {exc}"}
            receipt["elapsed_seconds"] = time.monotonic() - attempt_started
            receipt["finished"] = datetime.now(timezone.utc).isoformat()
            record.write_text(json.dumps(receipt, indent=2))
        if receipt["status"] != "completed":
            log_tail = (directory / "output.log").read_text(errors="replace")[-12000:]
            if re.search(r"(?:HTTP(?:Error)?[ :]+|status(?: code)?[ :]+)(?:401|403)\b", log_tail, re.I):
                raise RuntimeError(f"Experiment authorization failure; log: {receipt['log']}")
            raise ExperimentExecutionError(f"Experiment {job['id']} failed: {receipt.get('error')}; log: {receipt['log']}")
        return receipt

    def _run_experiment_check(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._resource_config()
        manifest = self._experiment_manifest()
        missing = [p for p in manifest["required_inputs"] if not self._experiment_input_path(p).exists()]
        if missing:
            raise AwaitingResults(f"Missing experiment inputs: {missing}. Supply real data and resume.")
        receipt = self._execute_experiment(manifest["smoke"])
        return NodeResult(receipt["log"], {"phase": "experiment_check", "smoke": receipt,
                                          "manifest": manifest})

    def _checked_manifest(self, state: WorkflowState) -> dict[str, Any]:
        manifest = self._experiment_manifest()
        if manifest != state.node_results.get("experiment_check", {}).get("manifest"):
            raise RuntimeError("manifest not smoke-checked; restart at experiment_check")
        return manifest

    def _run_experiment_run(self, state: WorkflowState, prompt: str) -> NodeResult:
        manifest = self._checked_manifest(state)
        receipts = []
        prior = state.experiment_completed_receipts or state.node_results.get("experiment_run", {}).get("jobs", [])
        completed = {item.get("id"): item for item in prior
                     if isinstance(item, dict) and item.get("status") in {"completed", "success"}}
        for job in manifest["jobs"]:
            if job.get("id") in completed:
                receipts.append(completed[job["id"]])
            else:
                receipts.append(self._execute_experiment(job))
            state.node_results["experiment_run"] = {
                "artifact": "experiment-runs", "phase": "experiment_run", "jobs": list(receipts),
                "deferred_jobs": manifest.get("deferred_jobs", []),
            }
            state.experiment_completed_receipts = list(receipts)
        if manifest.get("deferred_jobs"):
            if state.experiment_expansion_count >= 2:
                self._require_no_deferred_jobs(manifest)
            raise ExperimentExpansionRequired(
                "Data-preparation jobs completed; expanding deferred experiment matrix before execution.")
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
            self._require_file(relative)

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

    def _inspect_experiment_package(self) -> dict[str, Any]:
        files = [p for p in self._project.rglob("*")
                 if p.is_file() and ".git" not in p.parts]
        code = [p for p in files if p.suffix in {".py", ".sh", ".ipynb"}]
        docs = [p for p in files if p.name.lower() in {"readme.md", "experiment_readme.md"}]
        entry = [p for p in code if p.name in {"main.py", "run.py", "train.py", "experiment.py"}]
        return {"files": len(files), "code_files": len(code),
                "documentation": bool(docs), "entrypoint": bool(entry),
                "code_paths": [str(p.relative_to(self._project)) for p in code[:30]]}

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
        result = self.agent.go(
            f"Original task:\n{prompt}\n\nCreate the paper plan. "
            f"Available measured result files: {paths}.", phase="paper_plan")
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
        result = self.agent.go(
            f"Original task:\n{prompt}\n\nProduce the planned figures from PAPER_PLAN.md "
            "and results/ using the phase skill." + image_instruction, phase="paper_figure")
        self._require_file("figures/latex_includes.tex")
        self._require_file("figures/figure_plan.md")
        self._require_file("figures/figure_captions.md")
        metadata: dict[str, Any] = {"phase": "paper_figure", "iterations": result.iterations}
        # Only receipts returned by this invocation count. The runner never generates.
        images: dict[str, dict[str, Any]] = {}
        for message in getattr(result, "messages", []):
            if (message.get("role") != "tool" or message.get("name") != "generate_image"
                    or message.get("is_error")):
                continue
            job = json.loads(message.get("content", "{}"))
            if not isinstance(job, dict) or job.get("status") != "completed":
                raise RuntimeError("Invalid generate_image success receipt")
            relative = job.get("output_path")
            if not isinstance(relative, str):
                raise RuntimeError("Image receipt has no output_path")
            image_path = (self._project / relative).resolve()
            if not image_path.is_relative_to(self._project / "figures"):
                raise RuntimeError("Image receipt output must be inside project figures/")
            images[str(image_path.relative_to(self._project))] = job
        for relative, job in images.items():
            data = self._require_file(relative).read_bytes()
            if len(data) < 100 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(f"Image receipt output is not a PNG: {relative}")
            if hashlib.sha256(data).hexdigest() != job.get("sha256"):
                raise RuntimeError(f"Image changed after generate_image: {relative}")
        expected = "figures/figure_1_motivation_architecture.png"
        if self.require_image_figure and expected not in images:
            raise RuntimeError("Figure 1 requires a successful generate_image tool receipt from this invocation")
        if images:
            metadata.update(image_backend="project-mcp", images=list(images.values()))
        return NodeResult("figures/latex_includes.tex", metadata)

    def _run_paper_write(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("PAPER_PLAN.md")
        self._require_file("figures/latex_includes.tex")
        result = self.agent.go(
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
            attempts = 1
        except RuntimeError as exc:
            for attempts in range(2, 4):
                self.agent.go(
                    f"Fix the LaTeX compilation failure in paper/. If build tools or packages are missing, install them using bash and verify availability; otherwise read the LaTeX log and edit the source, "
                    f"then leave a buildable paper. Error:\n{exc}", phase="paper_compile")
                try:
                    pdf = self._compile_pdf()
                    break
                except RuntimeError as retry_exc:
                    exc = retry_exc
            else:
                raise RuntimeError(f"paper compilation failed after 3 attempts: {exc}") from exc
        return NodeResult(str(pdf.relative_to(self._project)), {"phase": "paper_compile", "attempts": attempts})

    def _run_paper_improvement(self, state: WorkflowState, prompt: str) -> NodeResult:
        self._require_file("paper/main.pdf")
        state.paper_status = "pdf_ready"
        paper = self._project / "paper"
        log = paper / "PAPER_IMPROVEMENT_LOG.md"
        progress = state.node_results.get("auto_paper_improvement_loop", {})
        completed_rounds = int(progress.get("completed_rounds", 0))
        if (completed_rounds < 0 or completed_rounds > self.config.max_paper_review_rounds
                or (completed_rounds and not log.is_file())):
            raise RuntimeError("saved paper review progress is inconsistent; inspect the improvement log")
        if not log.exists():
            log.write_text("# Paper improvement log\n", encoding="utf-8")

        def checkpoint(round_number: int, stage: str, **details: Any) -> None:
            state.node_results["auto_paper_improvement_loop"] = {
                "artifact": "paper/PAPER_IMPROVEMENT_LOG.md", "phase": "paper_improve",
                "completed_rounds": completed_rounds, "round": round_number,
                "stage": stage, **details,
            }
            if self.on_node_progress:
                self.on_node_progress("auto_paper_improvement_loop", state,
                                      state.node_results["auto_paper_improvement_loop"])

        for round_number in range(completed_rounds + 1, self.config.max_paper_review_rounds + 1):
            # review() starts a new Codex thread; no previous round's feedback is sent.
            review_prompt = (
                "Review paper/main.tex and paper/main.pdf as an independent scientific paper reviewer. "
                "Check claims against results/ and PAPER_PLAN.md. Return concrete, prioritized fixes "
                "for evidence, correctness, clarity, figures, and citations. Do not edit files."
            )
            stage = "reviewing"
            checkpoint(round_number, stage)
            try:
                if self.reviewer is not None:
                    review = self.reviewer.review(self._project, review_prompt)
                    feedback = review.get("content")
                    thread_id = review.get("threadId", "unknown")
                else:
                    tool_result = external_review(ToolContext(self._project), review_prompt)
                    if tool_result.is_error:
                        raise RuntimeError(tool_result.output)
                    payload = json.loads(tool_result.output)
                    feedback = payload.get("response")
                    thread_id = payload.get("threadId", "unknown")
                if not isinstance(feedback, str) or not feedback.strip():
                    raise RuntimeError(f"empty independent review in round {round_number}")
                stage = "improving"
                checkpoint(round_number, stage, reviewer_thread=thread_id, feedback=feedback)
                self.agent.go(
                    f"{prompt}\nImprove the paper using this independent review (round {round_number}). "
                    "Change only supported claims and writing/LaTeX. Do not invent evidence or citations. "
                    f"Review:\n{feedback}", phase="paper_improve")
                stage = "compiling"
                checkpoint(round_number, stage, reviewer_thread=thread_id, feedback=feedback)
                pdf = self._compile_pdf()
                snapshot = paper / f"main_round{round_number}.pdf"
                shutil.copy2(pdf, snapshot)
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"\n## Round {round_number}\n\nReviewer thread: {thread_id}\n\n{feedback}\n\nCompiled: {snapshot.name}\n")
                completed_rounds = round_number
                checkpoint(round_number, "completed", reviewer_thread=thread_id)
            except Exception as exc:
                checkpoint(round_number, "failed", error=str(exc))
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"\n## Round {round_number} interrupted\n\nStage: {stage}\n\nError: {exc}\n")
                raise
        return NodeResult("paper/PAPER_IMPROVEMENT_LOG.md", {"phase": "paper_improve", "rounds": self.config.max_paper_review_rounds})

    def _next_node(self, state: WorkflowState, node: str) -> str | None:
        if node == "literature":
            return "ideas"
        if node == "ideas":
            return "novelty"
        if node == "novelty":
            return "refine"
        if node == "refine":
            return "experiment_plan"
        if node == "experiment_plan":
            return "experiment_bridge"
        if node == "experiment_bridge":
            return "experiment_check"
        if node == "experiment_check":
            return "experiment_run"
        if node == "experiment_run":
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


def parse_review_result(agent_state: Any) -> ReviewResult:
    """Extract score/verdict from tool output or the final reviewer response."""
    candidates: list[str] = []
    reviewer_model: str | None = None
    structured_score: float | None = None
    structured_verdict: str | None = None
    details: dict[str, Any] = {}
    tool_error: str | None = None
    for message in getattr(agent_state, "messages", []):
        if message.get("role") != "tool" or message.get("name") != "external_review":
            continue
        content = str(message.get("content", ""))
        if message.get("is_error"):
            tool_error = content
        candidates.append(content)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            # External adapters sometimes prepend an error/status line while
            # still returning the review JSON. Recover the JSON object so a
            # valid local review is not discarded solely because the adapter
            # failed after producing its response.
            start, end = content.find("{"), content.rfind("}")
            payload = None
            if start >= 0 and end > start:
                try:
                    payload = json.loads(content[start:end + 1])
                except json.JSONDecodeError:
                    payload = None
        if isinstance(payload, dict):
            reviewer_model = payload.get("model") or payload.get("reviewer") or payload.get("reviewer_model")
            structured_score, structured_verdict = _review_fields(
                payload, structured_score, structured_verdict)
            response = payload.get("response")
            if isinstance(response, str):
                candidates.insert(0, response)
                try:
                    nested = json.loads(response)
                except json.JSONDecodeError:
                    nested = None
                if isinstance(nested, dict):
                    structured_score, structured_verdict = _review_fields(
                        nested, structured_score, structured_verdict)
                    if nested.get("score") is not None or nested.get("rating") is not None:
                        details = nested
            elif payload.get("score") is not None or payload.get("rating") is not None:
                details = payload
    final = getattr(agent_state, "final", "") or ""
    if final:
        candidates.append(final)
        try:
            payload = json.loads(final)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            structured_score, structured_verdict = _review_fields(
                payload, structured_score, structured_verdict)
            if payload.get("score") is not None or payload.get("rating") is not None:
                details = payload
    raw = "\n\n".join(candidates)
    score = structured_score if structured_score is not None else _find_score(raw)
    verdict_match = re.search(
        r"\b(PROCEED(?:\s+WITH\s+CAUTION)?|ABANDON|READY|REJECT(?:ED)?|"
        r"MAJOR[_ ]REVISION|PROMISING[_ ]BUT[_ ]UNVALIDATED|ACCEPT[_ ]AS[_ ]IDEA)\b", raw, re.I)
    verdict = structured_verdict or (verdict_match.group(1).upper().replace(" ", "_")
                                     if verdict_match else None)
    if verdict == "REJECTED":
        verdict = "REJECT"
    if score is not None and verdict is not None:
        parse_error = None
    elif tool_error:
        parse_error = f"external reviewer failed: {tool_error}"
    else:
        parse_error = "review did not contain score and verdict"
    return ReviewResult(score=score, verdict=verdict, reviewer_model=reviewer_model,
                        raw_response=raw, parse_error=parse_error, details=details)


def _review_fields(payload: dict[str, Any], score: float | None,
                   verdict: str | None) -> tuple[float | None, str | None]:
    value = payload.get("score", payload.get("rating"))
    if score is None:
        try:
            numeric = float(value)
            if 0 <= numeric <= 10:
                score = numeric
        except (TypeError, ValueError):
            pass
    candidate = payload.get("verdict")
    if verdict is None and isinstance(candidate, str):
        verdict = candidate.upper().replace(" ", "_")
        if verdict == "REJECTED":
            verdict = "REJECT"
    return score, verdict


def _find_score(text: str) -> float | None:
    match = re.search(r"(?:score|rating)\s*[:=\-]?\s*(\d+(?:\.\d+)?)\s*(?:/\s*10)?", text, re.I)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 10 else None


__all__ = ["ReviewResult", "WorkflowConfig", "WorkflowRunner", "WorkflowState", "parse_review_result"]
