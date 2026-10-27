"""Phase-aware function-calling runtime."""

from __future__ import annotations

import inspect
import json
import os
import sys
import types
import argparse
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from typing import Union, get_args, get_origin, get_type_hints

from paper_agent.tools import (ToolContext, ToolResult, ToolSpec,
                                builtin_tools, tools_for_phase)

from paper_agent.project_config import compute_context

from .model_client import ModelClient, ModelResponse, ToolCall, load_env_file

__all__ = ["AgentLoop", "PhaseAgent", "AgentState", "ArtifactValidationError", "ToolCall", "tool_schema", "main"]


class ArtifactValidationError(ValueError, RuntimeError):
    """A generated artifact failed a deterministic contract."""


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """Build an OpenAI function schema from a tool handler signature."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    signature = inspect.signature(spec.handler)
    try:
        annotations = get_type_hints(spec.handler)
    except (NameError, TypeError):
        annotations = {}
    for name, parameter in list(signature.parameters.items())[1:]:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = annotations.get(name, parameter.annotation)
        origin = get_origin(annotation)
        if origin in (Union, types.UnionType):
            non_none = [item for item in get_args(annotation) if item is not type(None)]
            annotation = non_none[0] if non_none else Any
            origin = get_origin(annotation)
        if annotation is bool:
            parameter_schema = {"type": "boolean"}
        elif annotation is int:
            parameter_schema = {"type": "integer"}
        elif annotation is float:
            parameter_schema = {"type": "number"}
        elif annotation in (dict, Any) or origin is dict:
            parameter_schema = {"type": "object"}
        elif origin in (list, tuple, set):
            item_type = get_args(annotation)[0] if get_args(annotation) else Any
            item_schema = {"type": "object" if get_origin(item_type) is dict or item_type is dict else "string"}
            parameter_schema = {"type": "array", "items": item_schema}
        else:
            parameter_schema = {"type": "string"}
        properties[name] = parameter_schema
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"type": "function", "function": {"name": spec.name,
            "description": spec.description, "parameters": schema}}


@dataclass
class AgentState:
    messages: list[dict[str, Any]]
    phase: str
    final: str = ""
    iterations: int = 0


class AgentLoop:
    # These are runtime-owned locations.  Callers choose the project and the
    # tool set; they should not have to wire the installation's skill/library
    # roots into every loop instance.
    DEFAULT_SKILL_DIRS = (Path(__file__).resolve().parents[1] / "skills",)
    DEFAULT_KNOWLEDGE_DIRS = (Path(__file__).resolve().parents[2] / "电池资源库",)

    def __init__(self, model: ModelClient, *, workspace: Path,
                 tools: tuple[ToolSpec, ...], max_iterations: int = 100,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None):
        self.model, self.workspace, self.tools = model, workspace, tools
        self.skill_dirs = self.DEFAULT_SKILL_DIRS
        self.knowledge_roots = tuple(
            path for path in self.DEFAULT_KNOWLEDGE_DIRS if path.is_dir()
        )
        self.max_iterations = max_iterations
        self.on_progress = on_progress
        self._by_name = {spec.name: spec for spec in tools}
        self.last_state: AgentState | None = None

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        result = spec.handler(ToolContext(self.workspace, self.skill_dirs, self.knowledge_roots), **arguments)
        if inspect.isawaitable(result):
            result = await result
        return result

    def run(self, prompt: str, *, system_prompt: str, phase: str,
                  completion_check: Callable[[], None] | None = None) -> AgentState:
        import asyncio
        return asyncio.run(self.arun(prompt, system_prompt=system_prompt, phase=phase,
                                     completion_check=completion_check))

    async def arun(self, prompt: str, *, system_prompt: str, phase: str,
                  completion_check: Callable[[], None] | None = None) -> AgentState:
        state = AgentState([{"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt}], phase)
        schemas = [tool_schema(spec) for spec in self.tools]
        self.last_state = state

        completion_retries = 1 if phase == "ideas" else 0
        for iteration in range(1, self.max_iterations + completion_retries + 1):
            state.iterations = iteration
            self._progress("model_start", phase=phase, iteration=iteration)
            self._write_phase_checkpoint(phase, iteration, "waiting_model")
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._model_heartbeat,
                args=(heartbeat_stop, phase, iteration),
                daemon=True,
            )
            heartbeat.start()
            try:
                response = self.model.complete(state.messages,
                                               schemas if iteration <= self.max_iterations else [])
            except Exception as exc:
                self._write_phase_checkpoint(phase, iteration, "model_error", error=str(exc))
                self._progress("model_error", phase=phase, iteration=iteration, error=str(exc))
                raise
            finally:
                heartbeat_stop.set()
            self._progress("model_end", phase=phase, iteration=iteration,
                           tools=[call.name for call in response.tool_calls])
            if response.content:
                self._progress("assistant_output", phase=phase, iteration=iteration,
                               content=response.content[:20000],
                               truncated=len(response.content) > 20000)
            self._write_phase_checkpoint(phase, iteration, "model_returned")
            if not response.tool_calls:
                try:
                    if completion_check is not None:
                        completion_check()
                    else:
                        self._check_completion(phase)
                except ArtifactValidationError as exc:
                    state.messages.extend([
                        {"role": "assistant", "content": response.content or ""},
                        {"role": "user", "content": f"Completion checks failed: {exc}. "
                         "Use the available tools to correct the artifact and verify it before finishing."},
                    ])
                    self._progress("validation_failed", phase=phase, iteration=iteration, error=str(exc))
                    if iteration >= self.max_iterations:
                        raise
                    continue
                state.final = response.content
                state.messages.append({"role": "assistant", "content": response.content})
                self.last_state = state
                self._write_phase_checkpoint(phase, iteration, "phase_completed")
                self._progress("phase_end", phase=phase, iteration=iteration)
                return state
            assistant = {"role": "assistant", "content": response.content or "", "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
                for call in response.tool_calls]}
            state.messages.append(assistant)
            visual_content = []
            for call in response.tool_calls:
                self._progress("tool_start", phase=phase, iteration=iteration, tool=call.name,
                               call_id=call.id,
                               arguments=json.dumps(call.arguments, ensure_ascii=False)[:6000])
                spec = self._by_name.get(call.name)
                if spec is None:
                    result = ToolResult(f"Tool not available in phase {phase}: {call.name}", True)
                else:
                    try:
                        result = await self._invoke(spec, call.arguments)
                    except Exception as exc:
                        result = ToolResult(str(exc), True)
                state.messages.append({"role": "tool", "tool_call_id": call.id,
                                       "name": call.name, "content": result.output,
                                       "is_error": result.is_error})
                if result.images and not result.is_error:
                    visual_content.append({"type": "text", "text":
                        f"Visual output of {call.name} ({call.id}): {result.output}"})
                    visual_content.extend(result.images)
                self._progress("tool_end", phase=phase, iteration=iteration,
                               tool=call.name, call_id=call.id, is_error=result.is_error,
                               output=result.output[:6000], truncated=len(result.output) > 6000)
            # Chat Completions accepts images in user content. Resolve every
            # outstanding tool call before adding the visual message.
            if visual_content:
                state.messages.append({"role": "user", "content": visual_content})
        self.last_state = state
        raise RuntimeError(f"model exceeded max_iterations={self.max_iterations} "
                           "(per phase-agent call; configure --max-tool-rounds, not workflow stages)")

    def _check_completion(self, phase: str) -> None:
        """Minimal artifact checks for direct calls without a workflow validator."""
        required = {
            "literature": ("literature/RESEARCH_REPORT.md",),
            "data_search": ("data-search/DATA_REPORT.md",),
            "ideas": ("idea-stage/IDEA_REPORT.md",),
            "paper_plan": ("PAPER_PLAN.md",),
            "paper_figure": ("figures/latex_includes.tex", "figures/figure_plan.md"),
            "paper_write": ("paper/main.tex", "paper/references.bib"),
        }.get(phase, ())
        missing = [name for name in required
                   if not (self.workspace / name).is_file()
                   or not (self.workspace / name).read_text(encoding="utf-8").strip()]
        if phase == "paper_write" and not any(
            path.is_file() and path.read_text(encoding="utf-8").strip()
            for path in (self.workspace / "paper/sections").glob("*.tex")
        ):
            missing.append("paper/sections/*.tex (at least one nonempty section)")
        if missing:
            raise ArtifactValidationError(f"{phase} cannot finish without: {', '.join(missing)}")

    def _model_heartbeat(self, stop: threading.Event, phase: str, iteration: int) -> None:
        """Emit durable liveness events while a synchronous model request is pending."""
        interval = 15.0
        while not stop.wait(interval):
            self._progress("model_heartbeat", phase=phase, iteration=iteration,
                           waited_seconds=round(interval, 1))
            self._write_phase_checkpoint(phase, iteration, "model_waiting")

    def _write_phase_checkpoint(self, phase: str, iteration: int, status: str,
                                *, error: str | None = None) -> None:
        """Persist a small resumable marker before and during long requests."""
        path = self.workspace / ".paper-agent" / "phase-progress" / f"{phase}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": phase, "iteration": iteration, "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            payload["error"] = error
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _progress(self, kind: str, **data: Any) -> None:
        if self.on_progress is not None:
            self.on_progress(kind, data)


class PhaseAgent(AgentLoop):
    """Single runtime for phase configuration and tool-calling execution.

    ``AgentLoop`` remains available as the low-level loop for callers that
    already provide a complete tool set.  Normal paper workflow code should
    use this class so phase selection, skill loading, and execution share one
    runtime instead of being split between a configurator and ``AgentLoop``.
    """

    SKILL_BY_PHASE = {
        "literature": "research-lit",
        "data_search": "data-search",
        "ideas": "idea-creator",
        "novelty": "novelty-check",
        "research_refine": "research-refine",
        "experiment_plan": "experiment-plan",
        "experiment_bridge": "experiment-bridge",
        "paper_plan": "paper-plan",
        "paper_figure": "paper-figure",
        "paper_write": "paper-write",
        "paper_compile": "paper-compile",
        "improvement": "auto-paper-improvement-loop",
    }
    DEFAULT_BATTERY_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[2] / "电池资源库"

    def __init__(self, project: str | Path, *, model: str | None = None,
                 model_client: ModelClient | None = None,
                 knowledge_root: str | Path | None = DEFAULT_BATTERY_KNOWLEDGE_ROOT,
                 phase: str = "ideas", max_iterations: int = 100,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float | None = None,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None):
        self.project = Path(project).resolve()
        repo_root = Path(__file__).resolve().parents[2]
        load_env_file(repo_root / ".env")
        load_env_file(repo_root / ".env.example")
        selected_model = model or os.environ.get("PAPER_AGENT_MODEL", "gpt-4o")
        self.model = model_client or ModelClient(selected_model, api_key=api_key,
                                                 base_url=base_url, timeout=timeout)
        self.skills_root = repo_root / "paper_agent" / "skills"
        self.knowledge_root = Path(knowledge_root).resolve() if knowledge_root else None
        self.phase = phase
        self.max_iterations = max_iterations
        self.on_progress = on_progress
        self._tools = {spec.name: spec for spec in builtin_tools()}
        self._custom_tools: set[str] = set()
        self._tool_phases: dict[str, tuple[str, ...]] = {}
        self._skills: list[str] = []
        self.configure()

    def add_tool(self, api: ToolSpec, *, phases: tuple[str, ...] | None = None) -> None:
        """Register a tool and rebuild the phase's visible tool set."""
        if callable(api) and not isinstance(api, ToolSpec):
            api = ToolSpec(api.__name__, getattr(api, "__doc__", "") or "", api)
        if not isinstance(api, ToolSpec):
            raise TypeError("api must be a ToolSpec or a callable accepting ToolContext")
        self._tools[api.name] = api
        self._custom_tools.add(api.name)
        if phases is not None:
            self._tool_phases[api.name] = phases
        else:
            self._tool_phases.pop(api.name, None)
        self.configure()

    def add_skill(self, name: str) -> None:
        if name not in self._skills:
            self._skills.append(name)
        self.configure()

    def configure(self, *, phase: str | None = None) -> None:
        if phase:
            self.phase = phase
        allowed = tools_for_phase(self.phase, tuple(self._tools.values()))
        selected_names = {spec.name for spec in allowed} | {
            name for name in self._custom_tools
            if name not in self._tool_phases or self.phase in self._tool_phases[name]
        }
        selected = tuple(spec for spec in self._tools.values() if spec.name in selected_names)
        # Reset the tool loop for the selected phase.
        AgentLoop.__init__(self, self.model, workspace=self.project, tools=selected,
                           max_iterations=self.max_iterations,
                           on_progress=self.on_progress)
        self.skill_dirs = (self.skills_root,)
        self.knowledge_roots = ((self.knowledge_root,) if self.knowledge_root else ())

    def _skill_prompt(self) -> str:
        names = self._skills or [self.SKILL_BY_PHASE.get(self.phase, self.phase)]
        text = []
        for name in names:
            path = self.skills_root / name / "SKILL.md"
            if path.is_file():
                text.append(f"\n## Skill: {name}\n{path.read_text(encoding='utf-8')}")
        return "".join(text)

    def _experiment_template_context(self) -> str:
        if self.phase not in {"experiment_plan", "experiment_bridge"}:
            return ""
        experiments = self.project / "experiments"
        directories = (sorted(path for path in experiments.iterdir()
                              if path.is_dir() and not path.name.startswith((".", "__")))
                       if experiments.is_dir() else [])
        return "\nProject experiment template context:\n" + json.dumps({
            "experiments_path": str(experiments),
            "candidate_directories": [str(path) for path in directories],
            "selection": "Confirm the base template from the project plan and directory contents.",
        }, ensure_ascii=False) + "\n"

    def _system_prompt(self) -> str:
        return ("You are an autonomous scientific paper workflow agent. "
                f"Current phase: {self.phase}. Write code and outputs only inside the project workspace. Read-only access to external datasets explicitly registered in battery resource README local-data fields is allowed. Use list_battery_data_sources, find and inspect_battery_data to verify real files before declaring data missing. Never modify source datasets, including via shell commands or generated scripts. "
                f"Runner Python executable: {sys.executable!r}. Manifest commands starting with python or python3 use this interpreter. "
                "When resuming experiments, verify that existing implementation and environment reports still match the current runtime and the approved experiment plan. Regenerate stale environment reports. An earlier CPU-only environment is not a reason to replace a planned CUDA model with a CPU/sklearn surrogate; restore the planned model and verify real CUDA training before collecting its results. Environment failures must be reported and fixed rather than changing the scientific method. "
                "When bash is available, install missing task dependencies into the actual execution environment and verify them. Dependency installation may write to that environment outside the workspace; source datasets remain read-only. "
                "Use tools for file access and scripts; do not claim an action was done until a tool result confirms it. "
                "Produce evidence-backed content and preserve existing artifacts. "
                "The local registered tools are authoritative; ignore ARIS slash commands, ARIS paths, and unavailable MCP tools mentioned in copied skill text. "
                + compute_context(self.project) + self._experiment_template_context()
                + self._skill_prompt())

    def go(self, prompt: str, *, phase: str | None = None,
           completion_check: Callable[[], None] | None = None) -> AgentState:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        self.project.mkdir(parents=True, exist_ok=True)
        self.configure(phase=phase)
        return self.run(prompt, system_prompt=self._system_prompt(), phase=self.phase,
                        completion_check=completion_check)


def main() -> None:
    """Run one workflow node from the command line."""
    parser = argparse.ArgumentParser(description="Run the paper agent tool-calling runtime")
    parser.add_argument("prompt", nargs="+", help="Task for the agent")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    from .workflow_cli import WORKFLOW_NODE_NAMES
    parser.add_argument("--phase", default="literature", choices=WORKFLOW_NODE_NAMES,
                        help="workflow node to run (stops after this node)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds",
                        type=int, default=100,
                        help="tool-enabled model rounds per phase-agent invocation (default: 100); not workflow stages")
    parser.add_argument("--timeout", type=float, default=None,
                        help="model request timeout in seconds (default: 300)")
    parser.add_argument("--idea-max-total-gpu-hours", type=float, default=8.0)
    parser.add_argument("--idea-search-paper-number", type=int, default=15)
    args = parser.parse_args()
    if args.max_tool_rounds < 1 or (args.timeout is not None and args.timeout <= 0):
        parser.error("--timeout and --max-tool-rounds must be positive")
    if not 0 < args.idea_max_total_gpu_hours < float("inf") or not 1 <= args.idea_search_paper_number <= 50:
        parser.error("idea GPU hours must be finite and positive; idea paper number must be 1-50")

    # Route the single-node command through the exact same workflow runner as
    # paper-workflow, so preprocessing, artifacts, state, and progress match.
    from .workflow_cli import run_workflow
    from .workflow import IdeaNodeConfig, WorkflowConfig

    state = run_workflow(
        args.project,
        " ".join(args.prompt),
        max_iterations=args.max_tool_rounds,
        timeout=args.timeout or 300,
        start_node=args.phase,
        stop_after=args.phase,
        model=args.model,
        idea_config=IdeaNodeConfig(args.idea_max_total_gpu_hours,
                                   args.idea_search_paper_number),
        config=WorkflowConfig(start_node="literature"),
    )
    print(f"状态: {state.status}; 当前节点: {state.current_node}")
    if state.error:
        print(f"错误: {state.error}")
    if state.status != "completed":
        raise SystemExit({"awaiting_results": 2}.get(state.status, 1))
