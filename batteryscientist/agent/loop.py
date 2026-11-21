"""Phase-aware function-calling runtime."""

from __future__ import annotations

import asyncio
import logging
import inspect
import itertools
import copy
import json
import os
import types
import argparse
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from typing import Literal, Union, get_args, get_origin, get_type_hints

from batteryscientist.tools import (ToolContext, ToolResult, ToolSpec,
                                builtin_tools, tools_for_phase)


from batteryscientist.integrations.model import ModelClient, ToolCall, load_env_file
from batteryscientist.agent.runtime.sessions import PhaseTask, RoleSession
from .context import CONTEXT_MAX_TOKENS, WorkingContext
from .stages import SKILL_BY_PHASE, WORKFLOW_NODE_NAMES, ROLE_DESCRIPTIONS

__all__ = ["AgentLoop", "PhaseAgent", "AgentState", "ArtifactValidationError", "ToolCall", "tool_schema", "main"]


logger = logging.getLogger(__name__)


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
        parameter_schema.update(spec.parameter_limits.get(name, {}))
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
    files: list[str] = field(default_factory=list)

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
        # Hard round limit for bounded loops. Auto-continuing agents use this
        # only as a progress reporting interval; it does not cap total rounds.
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
        return asyncio.run(self.arun(prompt, system_prompt=system_prompt, phase=phase,
                                     completion_check=completion_check, history=history))

    async def arun(self, prompt: str, *, system_prompt: str, phase: str,
                  completion_check: Callable[[], None] | None = None,
                  history: list[dict[str, Any]] | None = None) -> AgentState:
        if not self.connect_mcp:
            return await self._arun(prompt, system_prompt=system_prompt, phase=phase,
                                    completion_check=completion_check, history=history)
        from batteryscientist.integrations.mcp import MCPManager

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

        auto_continue = getattr(self, 'auto_continue_iterations', False)
        for iteration in itertools.count(1):
            if not auto_continue and iteration > self.max_iterations:
                break
            if auto_continue and iteration > 1 and (iteration - 1) % self.max_iterations == 0:
                self._progress('execution_continued', phase=phase, iteration=iteration,
                               segment=(iteration - 1) // self.max_iterations + 1)
            state.iterations = iteration
            self._progress("model_start", phase=phase, iteration=iteration)
            response = await self._request_model(state, schemas, working_context)
            self._prepare_tool_batch(response.tool_calls)
            self._progress("model_end", phase=phase, iteration=iteration,
                           tools=[call.name for call in response.tool_calls])
            if response.content:
                self._progress("assistant_output", phase=phase, iteration=iteration,
                               content=response.content[:20000],
                               truncated=len(response.content) > 20000)
            if not response.tool_calls:
                try:
                    if completion_check is not None:
                        completion_check()
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
                self._progress("phase_end", phase=phase, iteration=iteration)
                return state
            await self._execute_tools(state, response)
        self.last_state = state
        raise RuntimeError(f"model exceeded max_iterations={self.max_iterations} "
                           "(per phase-agent call; resume with a new assignment using existing artifacts)")

    async def _run_sync(self, function, *args, **kwargs):
        """Keep MCP responsive; finish an in-flight thread before unwinding its owner.

        Python cannot cancel the thread. Cancellation therefore waits for the
        synchronous request (including its configured timeout) to finish.
        """
        def invoke():
            # Transport interrupts as data so the helper task cannot stop the
            # event loop before its owning task has run session/MCP cleanup.
            try:
                return function(*args, **kwargs), None
            except BaseException as exc:
                return None, exc

        pending = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            result, error = await asyncio.shield(pending)
        except asyncio.CancelledError:
            _, error = await pending
            if error is not None:
                logger.error("Synchronous request failed while cancelling",
                             exc_info=(type(error), error, error.__traceback__))
            raise
        if error is not None:
            raise error
        return result

    async def _request_model(self, state, schemas, working_context):
        phase, iteration = state.phase, state.iterations
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._model_heartbeat,
            args=(heartbeat_stop, phase, iteration),
            daemon=True,
        )
        heartbeat.start()
        error_kind = "context_error"
        event_loop = asyncio.get_running_loop()
        try:
            if isinstance(self.model, ModelClient):
                self.model.usage_workspace = self.workspace
                self.model.usage_phase = phase
            previous_memory = state.context_memory
            request_messages = await self._run_sync(
                working_context.prepare,
                state, self.model, getattr(self, "context_max_tokens", CONTEXT_MAX_TOKENS),
                lambda: event_loop.call_soon_threadsafe(self._context_compaction_event, False, phase, iteration),
                tools=schemas, image_store=self.image_store,
                on_compacting=lambda: event_loop.call_soon_threadsafe(self._context_compaction_event, True, phase, iteration))
            if state.context_memory is not previous_memory:
                self._save_conversation(state)
            self._progress("context_prepared", phase=phase, iteration=iteration)
            stream_options = {}
            if isinstance(self.model, ModelClient):
                stream_options = {
                    'retry': self.retry_model_requests,
                    'on_delta': lambda text: event_loop.call_soon_threadsafe(
                        lambda: self._progress('assistant_delta', phase=phase, iteration=iteration, content=text)),
                    'on_stream_reset': lambda: event_loop.call_soon_threadsafe(
                        lambda: self._progress('assistant_stream_start', phase=phase, iteration=iteration)),
                }
            error_kind = "model_error"
            return await self._run_sync(self.model.complete, request_messages, schemas, **stream_options)
        except Exception as exc:
            logger.exception("%s (phase=%s, iteration=%s)", error_kind, phase, iteration)
            self._progress(error_kind, phase=phase, iteration=iteration, error=str(exc))
            raise
        finally:
            heartbeat_stop.set()

    async def _execute_tools(self, state, response):
        phase, iteration = state.phase, state.iterations
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
                    logger.exception("Tool %s failed (call_id=%s)", call.name, call.id)
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


    def _model_heartbeat(self, stop: threading.Event, phase: str, iteration: int) -> None:
        """Emit liveness events while a synchronous model request is pending."""
        interval = 15.0
        started = time.monotonic()
        while not stop.wait(interval):
            self._progress("model_heartbeat", phase=phase, iteration=iteration,
                           waited_seconds=round(time.monotonic() - started, 1),
                           context_compacting=getattr(self, "_context_compacting", False))

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
                 full_stage: bool = True,
                 required_outputs: tuple[str, ...] = (),
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
        selected_model = model or os.environ.get("BATTERYSCIENTIST_MODEL", "gpt-4o")
        self.model = model_client or ModelClient(selected_model, api_key=api_key,
                                                 base_url=base_url, timeout=timeout)
        self.skills_root = repo_root / "batteryscientist" / "skills"
        self.knowledge_root = Path(knowledge_root).resolve() if knowledge_root else None
        self.phase = phase
        if context_max_tokens < 1000:
            raise ValueError("context_max_tokens must be at least 1000")
        self.context_max_tokens = context_max_tokens
        self.role: str | None = None
        if assignment_role is not None and assignment_role not in ROLE_DESCRIPTIONS:
            raise ValueError(f"Unknown role: {assignment_role}")
        self.assignment_role = assignment_role
        self.full_stage = full_stage
        self.required_outputs = required_outputs
        self._active_session = None
        self.wiki_ingestion = None
        self._tools = {spec.name: spec for spec in builtin_tools()}
        self._custom_tools: set[str] = set()
        self._tool_phases: dict[str, tuple[str, ...]] = {}
        super().__init__(self.model, workspace=self.project, tools=(),
                         max_iterations=max_iterations, on_progress=on_progress)
        self.configure()

    def add_tool(self, api: ToolSpec, *, phases: tuple[str, ...] | None = None) -> None:
        """Register a tool and rebuild the phase's visible tool set."""
        if not isinstance(api, ToolSpec):
            raise TypeError("api must be a ToolSpec")
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
            from batteryscientist.tools.wiki.ingest import WikiIngestion
            if self.wiki_ingestion is None:
                self.wiki_ingestion = WikiIngestion(
                    self.project, model=self.model,
                    on_progress=lambda action, details: self._progress('wiki_progress', action=action, **details))
            selected += self.wiki_ingestion.tools()
        selected += (ToolSpec('submit_delivery',
                              '登记本次交付的真实项目文件，files 按主产物在前排列。只列本次任务相关文件，不扩展到整个目录；后续修改或新增交付后重新登记。最终回答仍需说明结果与限制。',
                              self._submit_delivery),)
        selected += (ToolSpec('request_user_decision',
                              '提问并等待回答；context_paths 填写本问题依赖的项目内文件相对路径，文件变化会使回答失效。自动模式仅限已查证的必要输入阻塞，必须填写blocking_reason。',
                              self._ask_user),)
        # Refresh phase capabilities; execute() reloads the assigned role's history.
        self.tools = selected
        self._by_name = {spec.name: spec for spec in selected}
        self.skill_dirs = (self.skills_root,)
        self.knowledge_roots = (
            (self.knowledge_root,) if self.knowledge_root and self.phase != "literature" else ()
        )

    def _ask_user(self, ctx, question: str, options: list[str] | None = None,
                  blocking_reason: str = '', context_paths: list[str] | None = None) -> ToolResult:
        from batteryscientist.agent.runtime.questions import UserQuestions
        from batteryscientist.project_config import AUTO_QUESTION_REQUIREMENT
        if self.interaction_mode == 'auto' and not blocking_reason.strip():
            return ToolResult(AUTO_QUESTION_REQUIREMENT, True)
        if not self._active_session:
            raise ValueError('Questions require an active role task')
        session, data, task = (self._active_session.session, self._active_session.data,
                              self._active_session.record)
        store = UserQuestions(self.user_question_project)
        value = store.create(question, role=self.role, task_id=task['id'], phase=self.phase, options=options,
                             blocking_reason=blocking_reason.strip(), context_paths=context_paths)
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
        from batteryscientist.agent.runtime.questions import UserQuestions
        try:
            return await asyncio.to_thread(spec.handler, None, **arguments)
        except asyncio.CancelledError:
            UserQuestions(self.user_question_project).interrupt_owned(os.getpid())
            raise

    def _skill_prompt(self) -> str:
        name = SKILL_BY_PHASE[self.phase]
        if name is None:
            return ""
        path = self.skills_root / name / "SKILL.md"
        prompt = f"\n## Skill: {name}\n{path.read_text(encoding='utf-8')}"
        if self.phase == "wiki":
            from batteryscientist.agent.wiki_role import draft_instructions
            prompt += draft_instructions()
        return prompt

    def _delivery_path(self, name: str) -> Path:
        path = (self.project / name).resolve()
        if not path.is_relative_to(self.project) or not path.is_file() or not path.read_bytes().strip():
            raise ValueError(f"交付文件不存在、为空或位于项目外：{name}")
        return path

    def _submit_delivery(self, ctx, files: list[str]) -> ToolResult:
        if self.last_state is None:
            raise ValueError("交付必须属于正在执行的任务")
        paths = [self._delivery_path(name).relative_to(self.project).as_posix() for name in files]
        self.last_state.files = list(dict.fromkeys(paths))
        return ToolResult(json.dumps({'files': self.last_state.files}, ensure_ascii=False))

    def _check_completion(self, phase: str) -> None:
        try:
            if phase == 'wiki':
                self.wiki_ingestion.check_completion()
            for name in self.required_outputs:
                self._delivery_path(name)
            if self.last_state is not None:
                for name in self.last_state.files:
                    self._delivery_path(name)
        except ValueError as exc:
            raise ArtifactValidationError(str(exc)) from exc

    def _system_prompt(self) -> str:
        from batteryscientist.project_config import interaction_instructions
        return (f"你是研究项目的执行 Agent。当前阶段：{self.phase}。\n"
                + (f"当前角色：{self.role}。{ROLE_DESCRIPTIONS[self.role]}\n" if self.role else "")
                + "按本次派单和当前 Skill 自行读取所需材料、完成交付；完成派单后停止，不自行推进下游阶段。"
                  "历史会话和角色窗口的讨论不构成本次执行授权。审批由主控接收，不自行批准。"
                  "使用当前已注册的工具；未获工具结果证实的操作不得声称已完成。\n"
                  "每次工具调用解决一个当前未确定的问题；已有结果及产物未变化时复用。"
                  "失败先判断参数、环境或执行原因，修正后再调用，不原样重复；"
                  "只在源码或输入变化、前次失败或仍有未解决问题时重新生成、编译或查看同一产物。\n"
                + "交付文件由本次任务决定，不为通过阶段检查创建无关文件。完成文件任务后用 submit_delivery 登记本次产物，主文件在前。\n"
                + ("Wiki任务的write/edit只允许当前wiki_prepare返回的暂存目录中的draft*.json；"
                   "发布使用wiki_publish，交付回执由工具生成，不另写wiki-delivery.json。\n"
                   if self.phase == 'wiki' else "")
                + self._skill_prompt()
                + ("\n本次是普通任务：Skill仅提供方法参考，不要求执行完整阶段。"
                   "以本次派单的目标、范围和交付要求为准，先读已有文件，再按需修改。"
                   "不补交无关文档、不重做整套产物、不自行启动额外实验、审稿或下游阶段。"
                   "需要编译或检查时按指令使用工具并验证真实结果，最后报告修改、产物路径和限制。\n"
                   if not self.full_stage else "")
                + "\n优先复用已有证据，围绕当前缺口检索，满足本次交付要求后停止。\n"
                + interaction_instructions(self.interaction_mode))

    def run(self, prompt: str, *, system_prompt: str, phase: str,
            completion_check: Callable[[], None] | None = None,
            prepare_completion: Callable[[], None] | None = None,
            history: list[dict[str, Any]] | None = None) -> AgentState:
        return asyncio.run(self.arun(
            prompt, system_prompt=system_prompt, phase=phase,
            completion_check=completion_check, prepare_completion=prepare_completion, history=history))

    async def arun(self, prompt: str, *, system_prompt: str, phase: str,
                   completion_check: Callable[[], None] | None = None,
                   prepare_completion: Callable[[], None] | None = None,
                   history: list[dict[str, Any]] | None = None) -> AgentState:
        def check_delivery():
            if prepare_completion is not None:
                prepare_completion()
            self._check_completion(phase)
            if completion_check is not None:
                completion_check()

        return await super().arun(prompt, system_prompt=system_prompt, phase=phase,
                                  completion_check=check_delivery, history=history)

    def go(self, prompt: str, *, phase: str | None = None,
           role: str | None = None,
           completion_check: Callable[[], None] | None = None,
           prepare_completion: Callable[[], None] | None = None) -> AgentState:
        return self.execute(PhaseTask(phase or self.phase, prompt, role or self.assignment_role),
                            completion_check=completion_check, prepare_completion=prepare_completion)

    def execute(self, task: PhaseTask, *,
                completion_check: Callable[[], None] | None = None,
                prepare_completion: Callable[[], None] | None = None) -> AgentState:
        """Execute one skill assignment in a durable, isolated role conversation."""
        self.project.mkdir(parents=True, exist_ok=True)
        # Each Wiki task owns a fresh curator; carry only explicit input options.
        wiki_options = None
        if task.phase == 'wiki':
            wiki_options = self.wiki_ingestion.options if self.wiki_ingestion is not None else {}
            self.wiki_ingestion = None
        self.configure(phase=task.phase)
        if wiki_options:
            self.wiki_ingestion.assign(**wiki_options)
        self.role = task.role
        session = RoleSession(self.project, task.role)
        record = {"id": task.id, "phase": task.phase, "instruction": task.instruction}
        record["full_stage"] = self.full_stage
        if self.required_outputs:
            record["required_outputs"] = list(self.required_outputs)
        self.last_state = None
        try:
            with session.task(record, model=self.model, max_tokens=self.context_max_tokens,
                              get_state=lambda: self.last_state,
                              on_compacting=lambda: self._context_compaction_event(True),
                              on_compacted=lambda: self._context_compaction_event(False)) as active:
                self._active_session = active
                self._progress("role_task_start", role=task.role, phase=task.phase,
                               session_id=active.data["session_id"], task_id=task.id)
                result = self.run(
                    f"Current assignment [{task.id}], phase={task.phase}, role={task.role}:\n{task.instruction}",
                    system_prompt=self._system_prompt(), phase=task.phase,
                    completion_check=completion_check, prepare_completion=prepare_completion, history=active.history)
                status = ('failed' if task.phase == 'wiki' and self.wiki_ingestion.receipt.get('status') == 'failed'
                          else 'completed')
                record.update(status=status, final=result.final)
                if status == 'failed':
                    record['error'] = self.wiki_ingestion.receipt.get('error')
                return result
        finally:
            session_id = self._active_session.data['session_id'] if self._active_session else None
            self._active_session = None
            if 'started_at' in record:
                self._progress("role_task_end", role=task.role, phase=task.phase,
                               session_id=session_id, task_id=task.id, status=record["status"])

    def _progress(self, kind: str, **data: Any) -> None:
        if self._active_session is not None:
            session_data, task = self._active_session.data, self._active_session.record
            data = {"role": self.role, "session_id": session_data["session_id"],
                    "task_id": task["id"], **data}
        super()._progress(kind, **data)

    def _save_conversation(self, state: AgentState) -> None:
        if self._active_session is None:
            return
        session, data, record = (self._active_session.session, self._active_session.data,
                                self._active_session.record)
        session.save_conversation(data, record, state)



def main() -> None:
    """Run one workflow node from the command line."""
    parser = argparse.ArgumentParser(description="Run the batteryscientist tool-calling runtime")
    parser.add_argument("prompt", nargs="+", help="Task for the agent")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--phase", default="literature", choices=WORKFLOW_NODE_NAMES,
                        help="workflow node to run (stops after this node)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tool-rounds", dest="max_tool_rounds",
                        type=int, default=300,
                        help="progress reporting interval in model rounds (default: 300); unfinished assignments continue automatically")
    parser.add_argument("--timeout", type=float, default=None,
                        help="model request timeout in seconds (default: 300)")
    args = parser.parse_args()
    if args.max_tool_rounds < 1 or (args.timeout is not None and args.timeout <= 0):
        parser.error("--timeout and --max-tool-rounds must be positive")

    # Route the single-node command through the exact same workflow runner as
    # batteryscientist-workflow, so preprocessing, artifacts, state, and progress match.
    from .workflow_cli import run_coordinated_workflow
    from .workflow import WorkflowConfig

    state = run_coordinated_workflow(
        args.project,
        " ".join(args.prompt),
        max_iterations=args.max_tool_rounds,
        timeout=args.timeout or 300,
        start_node=args.phase,
        stop_after=args.phase,
        model=args.model,
        config=WorkflowConfig(start_node="literature"),
    )
    print(f"状态: {state.status}; 当前节点: {state.current_node}")
    if state.error:
        print(f"错误: {state.error}")
    if state.status != "completed":
        raise SystemExit({"awaiting_results": 2}.get(state.status, 1))
