"""Phase-aware function-calling runtime."""

from __future__ import annotations

import inspect
import itertools
import copy
import json
import os
import types
import argparse
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from typing import Literal, Union, get_args, get_origin, get_type_hints

from paper_agent.tools import (ToolContext, ToolResult, ToolSpec,
                                builtin_tools, tools_for_phase)


from paper_agent.integrations.model import ModelClient, ToolCall, load_env_file
from paper_agent.agent.runtime.sessions import PhaseTask, RoleSession, ROLE_DESCRIPTIONS, now
from .context import CONTEXT_MAX_TOKENS, WorkingContext, request_tokens
from .stages import BY_PHASE, SKILL_BY_PHASE, WORKFLOW_NODE_NAMES

__all__ = ["AgentLoop", "PhaseAgent", "AgentState", "ArtifactValidationError", "ToolCall", "tool_schema", "main"]


class ArtifactValidationError(ValueError, RuntimeError):
    """A generated artifact failed a deterministic contract."""


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """Build an OpenAI function schema from a tool handler signature."""
    if spec.input_schema is not None:
        return {"type": "function", "function": {
            "name": spec.name, "description": spec.description,
            "parameters": copy.deepcopy(spec.input_schema)}}
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
        if origin is Literal:
            parameter_schema = {"type": "string", "enum": list(get_args(annotation))}
        elif annotation is bool:
            parameter_schema = {"type": "boolean"}
        elif annotation is int:
            parameter_schema = {"type": "integer"}
        elif annotation is float:
            parameter_schema = {"type": "number"}
        elif annotation in (dict, Any) or origin is dict:
            parameter_schema = {"type": "object"}
        elif origin in (list, tuple, set):
            item_type = get_args(annotation)[0] if get_args(annotation) else Any
            item_schema = {"type": (
                "object" if get_origin(item_type) is dict or item_type is dict
                else {int: "integer", float: "number", bool: "boolean"}.get(item_type, "string")
            )}
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
    role: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    task_start_index: int = 1
    context_memory: dict | None = None

    @property
    def task_messages(self) -> list[dict[str, Any]]:
        """Evidence from this assignment only, excluding historical tool receipts."""
        return self.messages[self.task_start_index:]


class AgentLoop:
    # These are runtime-owned locations.  Callers choose the project and the
    # tool set; they should not have to wire the installation's skill/library
    # roots into every loop instance.
    DEFAULT_SKILL_DIRS = (Path(__file__).resolve().parents[1] / "skills",)
    DEFAULT_KNOWLEDGE_DIRS = (Path(__file__).resolve().parents[2] / "电池资源库",)

    def __init__(self, model: ModelClient, *, workspace: Path,
                 tools: tuple[ToolSpec, ...], max_iterations: int = 300,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None,
                 connect_mcp: bool = True, retry_model_requests: bool = True):
        self.model, self.workspace, self.tools = model, workspace, tools
        from .image_store import ImageStore
        self.image_store = ImageStore(workspace)
        self.skill_dirs = self.DEFAULT_SKILL_DIRS
        self.knowledge_roots = tuple(
            path for path in self.DEFAULT_KNOWLEDGE_DIRS if path.is_dir()
        )
        self.max_iterations = max_iterations
        self.on_progress = on_progress
        self.connect_mcp = connect_mcp
        self.retry_model_requests = retry_model_requests
        self._by_name = {spec.name: spec for spec in tools}
        self.last_state: AgentState | None = None

    async def _invoke(self, spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        result = spec.handler(ToolContext(self.workspace, self.skill_dirs, self.knowledge_roots,
                                          on_progress=lambda action, details: self._progress(action, **details)), **arguments)
        if inspect.isawaitable(result):
            result = await result
        return result

    def run(self, prompt: str, *, system_prompt: str, phase: str,
                  completion_check: Callable[[], None] | None = None,
                  history: list[dict[str, Any]] | None = None) -> AgentState:
        import asyncio
        return asyncio.run(self.arun(prompt, system_prompt=system_prompt, phase=phase,
                                     completion_check=completion_check, history=history))

    async def arun(self, prompt: str, *, system_prompt: str, phase: str,
                  completion_check: Callable[[], None] | None = None,
                  history: list[dict[str, Any]] | None = None) -> AgentState:
        if not self.connect_mcp:
            return await self._arun(prompt, system_prompt=system_prompt, phase=phase,
                                    completion_check=completion_check, history=history)
        from paper_agent.integrations.mcp import MCPManager

        # Sessions and their transport tasks live in this run's event loop.
        # Never retain a session across separate asyncio.run() invocations.
        original_tools, original_by_name = self.tools, self._by_name
        async with MCPManager.from_config(phase=phase, allow_unavailable=True) as mcp:
            self.tools = original_tools + mcp.tools
            self._by_name = {spec.name: spec for spec in self.tools}
            if len(self._by_name) != len(self.tools):
                self.tools, self._by_name = original_tools, original_by_name
                raise ValueError("MCP tool name conflicts with an existing tool")
            try:
                if mcp.tools:
                    self._progress("mcp_connected", tools=[spec.name for spec in mcp.tools])
                if mcp.unavailable:
                    self._progress("mcp_unavailable", errors=mcp.unavailable)
                    system_prompt += (
                        "\n以下可选 MCP 服务在本次启动时连接失败，相关工具不可用：\n"
                        + "\n".join(mcp.unavailable)
                        + "\n可继续完成不依赖这些服务的工作；确需相关服务时说明阻塞原因，"
                        "不得声称已调用、伪造结果或绕过设备接口执行操作。")
                return await self._arun(prompt, system_prompt=system_prompt, phase=phase,
                                        completion_check=completion_check, history=history)
            finally:
                self.tools, self._by_name = original_tools, original_by_name

    async def _arun(self, prompt: str, *, system_prompt: str, phase: str,
                   completion_check: Callable[[], None] | None = None,
                   history: list[dict[str, Any]] | None = None) -> AgentState:
        state = AgentState([{"role": "system", "content": system_prompt}, *(history or []),
                            {"role": "user", "content": prompt}], phase)
        state.task_start_index = 1 + len(history or [])
        schemas = self._tool_schemas()
        self.last_state = state
        working_context = WorkingContext()
        self._save_conversation(state)

        completion_retries = 1 if phase == "ideas" else 0
        auto_continue = getattr(self, 'auto_continue_iterations', False)
        for iteration in itertools.count(1):
            if not auto_continue and iteration > self.max_iterations + completion_retries:
                break
            if auto_continue and iteration > 1 and (iteration - 1) % self.max_iterations == 0:
                state.messages.append({'role': 'user', 'content':
                    'Continue the same assignment from the saved artifacts and tool results above. '
                    'This is a new execution segment, not a new task. Do not repeat completed actions '
                    'or reread sources solely because a segment ended. Finish the remaining work.'})
                self._save_conversation(state)
                self._write_phase_checkpoint(phase, iteration, 'continuing')
                self._progress('execution_continued', phase=phase, iteration=iteration,
                               segment=(iteration - 1) // self.max_iterations + 1)
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
                if isinstance(self.model, ModelClient):
                    self.model.usage_workspace = self.workspace
                    self.model.usage_phase = phase
                previous_memory = state.context_memory
                request_messages = working_context.prepare(
                    state, self.model, getattr(self, "context_max_tokens", CONTEXT_MAX_TOKENS),
                    lambda: self._context_compaction_event(False, phase, iteration),
                    tools=schemas, image_store=self.image_store,
                    on_compacting=lambda: self._context_compaction_event(True, phase, iteration))
                if state.context_memory is not previous_memory:
                    self._save_conversation(state)
                self._progress("context_prepared", phase=phase, iteration=iteration,
                               transcript_chars=len(json.dumps(state.messages, ensure_ascii=False)),
                               request_chars=len(json.dumps(request_messages, ensure_ascii=False)),
                               context_budget_tokens=getattr(self, "context_max_tokens", CONTEXT_MAX_TOKENS),
                               request_tokens=request_tokens(request_messages, schemas))
                stream_options = {}
                if isinstance(self.model, ModelClient):
                    stream_options = {
                        'retry': self.retry_model_requests,
                        'on_delta': lambda text: self._progress('assistant_delta', phase=phase,
                                                               iteration=iteration, content=text),
                        'on_stream_reset': lambda: self._progress('assistant_stream_start', phase=phase,
                                                                 iteration=iteration),
                    }
                response = self.model.complete(request_messages,
                                               schemas if auto_continue or iteration <= self.max_iterations else [], **stream_options)
            except Exception as exc:
                self._write_phase_checkpoint(phase, iteration, "model_error", error=str(exc))
                self._progress("model_error", phase=phase, iteration=iteration, error=str(exc))
                raise
            finally:
                heartbeat_stop.set()
            self._prepare_tool_batch(response.tool_calls)
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
                    self._save_conversation(state)
                    self._progress("validation_failed", phase=phase, iteration=iteration, error=str(exc))
                    if not auto_continue and iteration >= self.max_iterations:
                        raise
                    continue
                state.final = response.content
                state.messages.append({"role": "assistant", "content": response.content})
                self.last_state = state
                self._save_conversation(state)
                self._write_phase_checkpoint(phase, iteration, "phase_completed")
                self._progress("phase_end", phase=phase, iteration=iteration)
                return state
            assistant = {"role": "assistant", "content": response.content or "", "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
                for call in response.tool_calls]}
            state.messages.append(assistant)
            self._save_conversation(state)
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
                self._save_conversation(state)
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
                self._save_conversation(state)
        self.last_state = state
        raise RuntimeError(f"model exceeded max_iterations={self.max_iterations} "
                           "(per phase-agent call; resume with a new assignment using existing artifacts)")

    def _save_conversation(self, state: AgentState) -> None:
        """Low-level ephemeral loops have no role session storage."""

    def _prepare_tool_batch(self, calls) -> None:
        """Allow an owner to apply execution limits before any tool runs."""

    def _context_compaction_event(self, active, phase=None, iteration=None):
        self._context_compacting = active
        if active:
            self._progress("context_compacting", phase=phase or getattr(self, 'phase', 'coordinator'),
                           iteration=iteration)
        else:
            self._working_context_compacted(phase or getattr(self, 'phase', 'coordinator'), iteration)

    def _working_context_compacted(self, phase: str, iteration: int) -> None:
        self._progress("working_context_compacted", phase=phase, iteration=iteration)

    def _tool_schemas(self) -> list[dict[str, Any]]:
        return [tool_schema(spec) for spec in self.tools]


    def _check_completion(self, phase: str) -> None:
        from .stages import validate_outputs
        if phase in BY_PHASE:
            try:
                validate_outputs(self.workspace, phase)
            except ValueError as exc:
                raise ArtifactValidationError(str(exc)) from exc

    def _model_heartbeat(self, stop: threading.Event, phase: str, iteration: int) -> None:
        """Emit durable liveness events while a synchronous model request is pending."""
        interval = 15.0
        started = time.monotonic()
        while not stop.wait(interval):
            self._progress("model_heartbeat", phase=phase, iteration=iteration,
                           waited_seconds=round(time.monotonic() - started, 1),
                           context_compacting=getattr(self, "_context_compacting", False))
            self._write_phase_checkpoint(phase, iteration,
                                         "context_compacting" if getattr(self, "_context_compacting", False)
                                         else "model_waiting")

    def _write_phase_checkpoint(self, phase: str, iteration: int, status: str,
                                *, error: str | None = None) -> None:
        """Persist a small resumable marker before and during long requests."""
        path = self.workspace / ".paper-agent" / "phase-progress" / f"{phase}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": phase, "iteration": iteration, "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.last_state is not None and self.last_state.role is not None:
            payload.update(role=self.last_state.role, session_id=self.last_state.session_id,
                           task_id=self.last_state.task_id)
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
    """Role sessions executing phase-scoped skills and tools.

    ``AgentLoop`` remains available as the low-level loop for callers that
    already provide a complete tool set.  Normal paper workflow code should
    use this class so phase selection, skill loading, and execution share one
    runtime instead of being split between a configurator and ``AgentLoop``.
    """

    SKILL_BY_PHASE = SKILL_BY_PHASE
    SEARCH_BUDGETS = {
        "wiki": {"search_wiki": 5},
        "literature": {"search_wiki": 10},
        "data_search": {"search_wiki": 3, "battery_resources": 5},
        "ideas": {"search_wiki": 10, "battery_resources": 5},
        "research_refine": {"search_wiki": 3, "battery_resources": 3},
        "experiment_plan": {"search_wiki": 5, "battery_resources": 5},
        "experiment_bridge": {"search_wiki": 3, "battery_resources": 3},
        "paper_plan": {"search_wiki": 3},
        "paper_figure": {"search_wiki": 3, "battery_resources": 3},
        "paper_write": {"search_wiki": 5},
        "paper_compile": {"search_wiki": 3},
        "improvement": {"search_wiki": 3},
    }
    auto_continue_iterations = True
    DEFAULT_BATTERY_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[2] / "电池资源库"

    def __init__(self, project: str | Path, *, model: str | None = None,
                 model_client: ModelClient | None = None,
                 knowledge_root: str | Path | None = DEFAULT_BATTERY_KNOWLEDGE_ROOT,
                 phase: str = "ideas", max_iterations: int = 300,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float | None = None,
                 context_max_tokens: int = CONTEXT_MAX_TOKENS,
                 assignment_role: str | None = None,
                 user_question_project: str | Path | None = None,
                 interaction_mode: str = 'expert',
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None):
        self.project = Path(project).resolve()
        if interaction_mode not in {'auto', 'expert'}:
            raise ValueError('interaction_mode must be auto or expert')
        self.interaction_mode = interaction_mode
        self.user_question_project = Path(user_question_project).resolve() if user_question_project else self.project
        repo_root = Path(__file__).resolve().parents[2]
        load_env_file(repo_root / ".env")
        load_env_file(repo_root / ".env.example")
        selected_model = model or os.environ.get("PAPER_AGENT_MODEL", "gpt-4o")
        self.model = model_client or ModelClient(selected_model, api_key=api_key,
                                                 base_url=base_url, timeout=timeout)
        self.skills_root = repo_root / "paper_agent" / "skills"
        self.knowledge_root = Path(knowledge_root).resolve() if knowledge_root else None
        self.phase = phase
        if context_max_tokens < 1000:
            raise ValueError("context_max_tokens must be at least 1000")
        self.context_max_tokens = context_max_tokens
        self.role: str | None = None
        if assignment_role is not None and assignment_role not in ROLE_DESCRIPTIONS:
            raise ValueError(f"Unknown role: {assignment_role}")
        self.assignment_role = assignment_role
        self._active_session = None
        self.max_iterations = max_iterations
        self.on_progress = on_progress
        self._tools = {spec.name: spec for spec in builtin_tools()}
        self._custom_tools: set[str] = set()
        self._tool_phases: dict[str, tuple[str, ...]] = {}
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

    def configure(self, *, phase: str | None = None) -> None:
        if phase:
            self.phase = phase
        allowed = tools_for_phase(self.phase, tuple(self._tools.values()))
        selected_names = {spec.name for spec in allowed} | {
            name for name in self._custom_tools
            if name not in self._tool_phases or self.phase in self._tool_phases[name]
        }
        selected = tuple(spec for spec in self._tools.values() if spec.name in selected_names)
        if self.phase == 'wiki':
            from paper_agent.tools.wiki_ingest import WikiIngestion
            if not hasattr(self, 'wiki_ingestion'):
                self.wiki_ingestion = WikiIngestion(
                    self.project, model=self.model,
                    on_progress=lambda action, details: self._progress('wiki_progress', action=action, **details))
            selected += self.wiki_ingestion.tools()
        selected += (ToolSpec('request_user_decision',
                              '提问并等待回答；自动模式仅限已查证的必要输入阻塞，必须填写blocking_reason。',
                              self._ask_user),)
        # Refresh phase capabilities; execute() reloads the assigned role's history.
        AgentLoop.__init__(self, self.model, workspace=self.project, tools=selected,
                           max_iterations=self.max_iterations,
                           on_progress=self.on_progress)
        self.skill_dirs = (self.skills_root,)
        self.knowledge_roots = (
            (self.knowledge_root,) if self.knowledge_root and self.phase != "literature" else ()
        )

    def _ask_user(self, ctx, question: str, options: list[str] | None = None,
                  blocking_reason: str = '') -> ToolResult:
        from paper_agent.agent.runtime.questions import UserQuestions, AUTO_QUESTION_REQUIREMENT
        if self.interaction_mode == 'auto' and not blocking_reason.strip():
            return ToolResult(AUTO_QUESTION_REQUIREMENT, True)
        if not self._active_session:
            raise ValueError('Questions require an active role task')
        session, data, task = self._active_session
        store = UserQuestions(self.user_question_project)
        value = store.create(question, role=self.role, task_id=task['id'], phase=self.phase, options=options,
                             blocking_reason=blocking_reason.strip())
        task.update(status='awaiting_user', question_id=value['id'])
        session.save(data)
        answer = store.wait(value, self._progress)
        task.update(status='running')
        session.save(data)
        return ToolResult(json.dumps({'question_id': value['id'], 'question': question,
                                      'answer': answer, 'task_id': task['id']}, ensure_ascii=False))

    async def _invoke(self, spec, arguments):
        if self.phase == 'wiki' and spec.name in {'write', 'edit'}:
            try:
                self.wiki_ingestion.validate_draft_write(arguments.get('path', ''))
            except ValueError as exc:
                return ToolResult(str(exc), True)
        if spec.name != 'request_user_decision':
            return await super()._invoke(spec, arguments)
        import asyncio
        from paper_agent.agent.runtime.questions import UserQuestions
        try:
            return await asyncio.to_thread(spec.handler, None, **arguments)
        except asyncio.CancelledError:
            UserQuestions(self.user_question_project).interrupt_owned(os.getpid())
            raise

    def _skill_prompt(self) -> str:
        name = self.SKILL_BY_PHASE.get(self.phase, self.phase)
        if name is None:
            return ""
        path = self.skills_root / name / "SKILL.md"
        if path.is_file():
            prompt = f"\n## Skill: {name}\n{path.read_text(encoding='utf-8')}"
            if self.phase == "wiki":
                from paper_agent.agent.wiki_role import draft_instructions
                prompt += draft_instructions()
            return prompt
        return ""

    def _check_completion(self, phase: str) -> None:
        if phase == 'wiki':
            try:
                self.wiki_ingestion.check_completion()
            except ValueError as exc:
                raise ArtifactValidationError(str(exc)) from exc
            return
        super()._check_completion(phase)

    def _search_policy_prompt(self) -> str:
        budgets = self.SEARCH_BUDGETS[self.phase]
        limits = "\n".join(
            "- {}：本次派单累计最多调用 {} 次。".format(
                'battery_resources(action="search")' if name == 'battery_resources' else name, limit)
            for name, limit in budgets.items() if name in self._by_name
        )
        if "search_figure_references" in self._by_name:
            limits += "\n- search_figure_references：每张待绘制的图最多调用 2 次。"
        return (
            f"\n角色执行的检索与交付规则（当前阶段 {self.phase}，优先于 Skill 中冲突的探索要求）：\n"
            + limits
            + "\n额度仅统计上述搜索操作，定向阅读说明和检查已定位的样本不计入搜索次数。"
              "额度是上限，不是必须用满的目标。每个搜索调用计一次，失败或无结果也计入；"
              "同一轮模型响应中的多次调用分别计数。仅统计本次阶段派单的调用，历史其他任务不占额度；"
              "上下文压缩、重新加载 Skill、更换关键词或重新阅读记录不重置额度。\n"
              "优先复用已有报告和本次已读正文。新增搜索应回答会影响当前交付的具体缺口；"
              "Wiki 先用 search_wiki(view=\"titles\") 筛标题，需要时用 view=\"abstracts\" 看已有摘要；选定后用 read_wiki(wiki_path) 一次读取完整 MD，不再用通用 read 补读同一页面。已有文件路径时定向读取相关内容，不为重新定位同一论文反复搜索。"
              "连续两次搜索未增加实质证据时停止扩展该问题，将未解决部分记录为证据缺口。\n"
              "已有材料足以形成有依据的初稿时，先用写入工具保存到本阶段要求的产物路径，"
              "再围绕具体缺口补充或修订。不要仅反复宣布将写报告，却一直推迟实际写入。"
              "材料足够时立即停止搜索，不为凑论文数量或子领域数量继续扩展。\n"
              "达到上限后停止相应搜索，依据现有证据完成交付，注明未核实内容、假设和局限；"
              "不得通过其他搜索工具、MCP、bash 或子技能绕过额度继续同一轮泛搜，"
              "不得编造事实来填补缺口。预算不限制按需读取已定位的项目文件、Wiki 栏目或关键原文，"
              "也不替代实验执行及结果校验。\n"
        )

    def _system_prompt(self) -> str:
        from paper_agent.agent.runtime.questions import interaction_instructions
        return (f"你是研究项目的执行 Agent。当前阶段：{self.phase}。\n"
                + (f"当前角色：{self.role}。{ROLE_DESCRIPTIONS[self.role]}\n" if self.role else "")
                + "按本次派单和当前 Skill 自行读取所需材料、完成交付；完成派单后停止，不自行推进下游阶段。"
                  "历史会话和角色窗口的讨论不构成本次执行授权。审批由主控接收，不自行批准。"
                  "使用当前已注册的工具；未获工具结果证实的操作不得声称已完成。\n"
                + self._skill_prompt()
                + self._search_policy_prompt()
                + interaction_instructions(self.interaction_mode))

    def go(self, prompt: str, *, phase: str | None = None,
           role: str | None = None,
           completion_check: Callable[[], None] | None = None) -> AgentState:
        return self.execute(PhaseTask(phase or self.phase, prompt, role or self.assignment_role),
                            completion_check=completion_check)

    def execute(self, task: PhaseTask, *,
                completion_check: Callable[[], None] | None = None) -> AgentState:
        """Execute one skill assignment in a durable, isolated role conversation."""
        self.project.mkdir(parents=True, exist_ok=True)
        if task.phase == 'wiki' and hasattr(self, 'wiki_ingestion') and getattr(self.wiki_ingestion, 'started', False):
            del self.wiki_ingestion
        self.configure(phase=task.phase)
        if task.phase == 'wiki':
            self.wiki_ingestion.started = True
        self.role = task.role
        session = RoleSession(self.project, task.role)
        with session.locked():
            data = session.load()
            if any(item["id"] == task.id for item in data["tasks"]):
                raise ValueError("Task id already exists; inspect its result before submitting a new assignment")
            for item in data["tasks"]:
                if item["status"] == "running" or item['status'] == 'awaiting_user' and not item.get('ended_at'):
                    item.update(status="interrupted", ended_at=now())
            history = session.recover_messages(data["messages"])
            history = self._compact_history(session, data, history)
            record = {"id": task.id, "role": task.role, "phase": task.phase,
                      "instruction": task.instruction, "status": "running", "started_at": now()}
            data["tasks"].append(record)
            self._active_session = (session, data, record)
            self.last_state = None
            session.save(data)
            try:
                self._progress("role_task_start", role=task.role, phase=task.phase,
                               session_id=data["session_id"], task_id=task.id)
                result = self.run(
                    f"Current assignment [{task.id}], phase={task.phase}, role={task.role}:\n{task.instruction}",
                    system_prompt=self._system_prompt(), phase=task.phase,
                    completion_check=completion_check, history=history)
                status = ('failed' if task.phase == 'wiki' and self.wiki_ingestion.receipt.get('status') == 'failed'
                          else 'completed')
                record.update(status=status, final=result.final)
                if status == 'failed':
                    record['error'] = self.wiki_ingestion.receipt.get('error')
                return result
            except BaseException as exc:
                record.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                              error=str(exc))
                raise
            finally:
                record["ended_at"] = now()
                if self.last_state is not None:
                    self._save_conversation(self.last_state)
                else:
                    session.save(data)
                self._active_session = None
                self._progress("role_task_end", role=task.role, phase=task.phase,
                               session_id=data["session_id"], task_id=task.id, status=record["status"])

    def _progress(self, kind: str, **data: Any) -> None:
        if self._active_session is not None:
            _, session_data, task = self._active_session
            data = {"role": self.role, "session_id": session_data["session_id"],
                    "task_id": task["id"], **data}
        super()._progress(kind, **data)

    def _save_conversation(self, state: AgentState) -> None:
        if self._active_session is None:
            return
        session, data, record = self._active_session
        state.role, state.session_id, state.task_id = self.role, data["session_id"], record["id"]
        data["messages"] = [message for message in state.messages if message["role"] != "system"]
        data["context_memory"] = state.context_memory
        record["iterations"] = state.iterations
        session.save(data)

    def _compact_history(self, session: RoleSession, data: dict,
                         history: list[dict]) -> list[dict]:
        return session.compact(data, history, self.model, self.context_max_tokens,
                               on_compacting=lambda: self._context_compaction_event(True),
                               on_compacted=lambda: self._context_compaction_event(False))



def main() -> None:
    """Run one workflow node from the command line."""
    parser = argparse.ArgumentParser(description="Run the paper agent tool-calling runtime")
    parser.add_argument("prompt", nargs="+", help="Task for the agent")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--phase", default="literature", choices=WORKFLOW_NODE_NAMES,
                        help="workflow node to run (stops after this node)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds",
                        type=int, default=300,
                        help="model rounds per saved execution segment (default: 300); unfinished assignments continue automatically")
    parser.add_argument("--timeout", type=float, default=None,
                        help="model request timeout in seconds (default: 300)")
    parser.add_argument("--idea-max-total-gpu-hours", type=float, default=8.0)
    args = parser.parse_args()
    if args.max_tool_rounds < 1 or (args.timeout is not None and args.timeout <= 0):
        parser.error("--timeout and --max-tool-rounds must be positive")
    if not 0 < args.idea_max_total_gpu_hours < float("inf"):
        parser.error("idea GPU hours must be finite and positive")

    # Route the single-node command through the exact same workflow runner as
    # paper-workflow, so preprocessing, artifacts, state, and progress match.
    from .workflow_cli import run_coordinated_workflow
    from .workflow import IdeaNodeConfig, WorkflowConfig

    state = run_coordinated_workflow(
        args.project,
        " ".join(args.prompt),
        max_iterations=args.max_tool_rounds,
        timeout=args.timeout or 300,
        start_node=args.phase,
        stop_after=args.phase,
        model=args.model,
        idea_config=IdeaNodeConfig(max_total_gpu_hours=args.idea_max_total_gpu_hours),
        config=WorkflowConfig(start_node="literature"),
    )
    print(f"状态: {state.status}; 当前节点: {state.current_node}")
    if state.error:
        print(f"错误: {state.error}")
    if state.status != "completed":
        raise SystemExit({"awaiting_results": 2}.get(state.status, 1))
