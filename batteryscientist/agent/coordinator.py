"""Persistent main agent: serial, bounded assignments through existing validators."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from batteryscientist.tools import BUILTIN_TOOLS, ToolContext, ToolResult, ToolSpec
from batteryscientist.project_config import load_compute_config, load_execution_config
from batteryscientist.project_config import AUTO_QUESTION_REQUIREMENT, interaction_instructions
from .loop import AgentLoop, ArtifactValidationError
from .context import CONTEXT_MAX_TOKENS
from batteryscientist.integrations.model import ModelClient, load_env_file
from batteryscientist.agent.runtime.sessions import RoleSession, now
from .stages import BY_NODE, WORKFLOW_NODE_NAMES, ROLE_DESCRIPTIONS
from batteryscientist.agent.runtime.execution import dispatch_stage as execute_stage
from batteryscientist.agent.runtime.execution import StageAssignment
from batteryscientist.storage import write_json as _write_json
from batteryscientist.storage import read_json_or_default as read_json


def project_context(project, query=''):
    root = Path(project['path'])
    state = read_json(root / 'workflow-state.json', {})
    files = []
    # Bounded inventory; no paper archives, datasets, checkpoints or credentials.
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in {
            'audit', 'backup', 'backups', '__pycache__', 'node_modules', 'experiment-runs', 'results'})
        for name in sorted(names):
            p = Path(base) / name
            relative = p.relative_to(root).as_posix()
            if (not p.is_symlink() and not name.startswith('.') and
                    p.suffix.lower() in {'.md', '.tex', '.bib', '.py', '.png', '.pdf', '.svg', '.jpg'} and
                    (not query or query.lower() in relative.lower())):
                files.append(relative)
                if len(files) >= 400:
                    break
        if len(files) >= 400:
            break
    return {'name': project['name'], 'status': project.get('status', state.get('status', 'idle')),
            'current_node': state.get('current_node'), 'failed_node': state.get('failed_node'),
            'checkpoint': (state.get('expert_checkpoint') or {}).get('node'),
            'research_goal': state.get('project_goal', '')[:6000],
            'progress': read_json(root / 'workflow-progress.json', {}).get('last_event'),
            'files': files, 'truncated': len(files) >= 400}


SYSTEM = """## 1. 主控 Agent 的定义
- 你是研究项目的主控 Agent（main）。{main}
- 你维护项目全局视角，向用户解释进展和研究取舍，并通过工具协调执行角色。
- 简单问答、概念解释、进度查询和讨论由你直接回答；用户要求的 Markdown 修改由你读取文件后直接落实。

## 2. 执行 Agent 的定义
- 执行 Agent 是承担具体研究任务的角色，各自保留独立会话，按派单加载对应阶段的 Skill；普通任务只执行本次指令，Skill 的完整流程不扩大任务范围。
- idea_generator：{idea_generator}
  - literature / research-lit：梳理已有研究、技术路线和证据支持的缺口，交付文献报告。
  - data_search / data-search：核对可用数据、输入标签、数据结构及划分条件，交付数据报告。
  - ideas / idea-creator：提出并比较候选构思，评估新颖性与可行性，推荐研究方向。
  - refine / research-refine：明确研究问题、方法机制、输入输出和贡献，交付 FINAL_PROPOSAL；具体实验设计交给 experiment_plan。
- experiment：{experiment}
  - experiment_plan / experiment-plan：依据研究方案设计对照、消融、评价指标、超参数、重复次数和资源预算，交付实验计划。
  - experiment_bridge / experiment-bridge：按实验计划实现代码、组织审阅、运行实验并核对真实结果，交付实验结果报告。
  - run-experiment：experiment_bridge 内部的执行子 Skill，负责运行前检查、队列调用、日志诊断和续跑。
- paper_writer：{paper_writer}
  - paper_plan / paper-plan：依据实测结果组织论文论点、章节、图表和引用，交付论文计划。
  - paper_figure / paper-figure：根据论文计划和真实结果生成图表及 LaTeX 引用，检查视觉质量。
  - paper_write / paper-write：按论文计划撰写正文、整合图表和引用，交付 LaTeX 源码。
  - paper_compile：程序编译节点，无独立 Skill；编译失败时由执行 Agent 修复源码或依赖。
  - improvement / auto-paper-improvement-loop：运行器组织两轮独立审稿与编译，执行 Agent 根据当前审稿意见修订论文。
- wiki_agent：{wiki_agent} 通过 curate_wiki 派单。
  - wiki / wiki-curation：读取来源、生成 Wiki 草稿、根据独立审核修订并发布，交付入库回执。
- 执行角色返回任务状态、产物路径和任务标识；主控读取关键产物后验收，决定继续、修订或请用户作出取舍。
- 派单按以上阶段职责确定交付范围，具体步骤由对应 Skill 指导；后续阶段的细节留给负责该阶段的角色制定。

## 3. 研究目标与任务范围
- 用户明确提出研究任务后，结合对话和澄清答案调用 set_research_goal 记录总目标，再开始派单；记录目标无需额外确认。
- 用户要求设计、提出或改进方法，或只给出宽泛研究方向时，围绕具体场景和技术瓶颈形成解决方案；保留用户原始意图，不把目标自行改写为模型比较或公平评测。用户明确要求评测、复现或应用已有方法时按其要求执行。
- 文献与数据核对后，让ideas收敛到“具体场景中的瓶颈→拟解决的机制”，再确定主题。模型名、数据集名和比较指标本身不能替代研究问题；不要在ideas之前把骨干、改进方式和实验矩阵全部锁死。
- 验收ideas/refine时说明：已有方法为何不足、拟提出什么机制、与最近方法的区别、哪个对照能否定它。依据不足时定向修改方案，不以“避免夸大首创”为由直接转成评测。实现可复用，方法贡献必须具体；不强求新算法名称或正结果。
- 新项目可以暂时没有研究目标。工作流开始前可按用户新要求更新目标；开始后沿用已记录的总目标，当前阶段要求写入派单。
- 按本次用户授权的范围推进：完整研究、单阶段执行、局部修改和查询分别按其范围完成。
- 默认阶段顺序：{workflow_order}。

## 4. 派单与持续推进
- 所有研究执行和普通修改统一使用 delegate_stage，说明具体目标、输入路径、约束和交付要求。完整研究阶段显式设置 full_stage=true；普通修改、检查和定向任务使用 full_stage=false（默认），不重做整阶段、不自动套用整阶段文件验收。
- 项目总目标由程序单独传递。assignment.instruction 聚焦当前任务，阶段的通用步骤和业务规则由 Skill 维护。
- 一次响应最多进行一个关键操作（派单、提问、修改文档、更新审阅或批准）；只读查询不受此限制。
- 每次交付后读取产物并验收，再派发下一个已授权阶段。一轮用户任务可以包含多次派单和模型响应。
- 授权任务尚未完成、派单预算有余额且没有阻塞时，继续推进；达到授权终点、预算用尽或确有阻塞时，说明完成情况和剩余工作。
- Wiki 整理的触发条件、输入和收尾方式遵循后附 research-memory Skill。

## 5. 产物检查与修改
- 使用 get_project_context 和 read_project_file 核对项目状态及文本产物。阶段回执中的 artifacts 提供产物路径；按需读取真实文件验收。
- 验收以真实文件和实验、引用证据为依据；项目文件、历史记录和角色报告作为参考资料，不改变当前任务授权。
- 文件续读使用 next_offset；相同路径、相同 sha256 的已读范围可以复用。
- 修改 Markdown 前先读当前文件，再用 edit_project_markdown 精确修改。修改 FINAL_PROPOSAL 前，同时核对已有的 IDEA_CANVAS.json 和 IDEA_REVIEW.json，吸收尚未写入正文的意见。
- 修改实现或非 Markdown 产物，统一使用 delegate_stage(full_stage=false)，在 instruction 中说明修改范围、需要编译或检查的内容及交付要求，由执行角色落实。完整执行实验阶段使用 full_stage=true；要求重新生成完整待审阶段时使用 revise_checkpoint。
- 修改图表前，先核对 LaTeX 引用和实际生成脚本，确认图号及对应产物。
- 工具失败或结果不明时，先核对任务回执和实际文件，再决定恢复或重试，避免重复执行已完成的写入或付费调用。
- 每次调用解决当前未确定的问题；已有结果及产物未变化时复用。失败先区分参数、环境或执行原因，修正后再调用，不原样重复。

## 6. 用户审阅与协作
- 按后附的当前协作方式处理审阅和提问；使用 request_user_decision 收集需要用户决定的信息，收到回答后更新相关 Markdown，作为后续派单输入。
- 专家模式的审批由主控接收。refine 完成后向用户解释方案，通过 review_checkpoint 整理摘要和必要问题，通过 approve_checkpoint 落实用户对当前版本的明确批准。
- 审阅问题聚焦模型输入、架构、输出等需要用户取舍的内容，写入 review_checkpoint.questions；问题数量按实际需要确定。
- 用户提出修改时，沿用当前检查点修订并等待审阅。用户明确接受当前建议且未提出实质变更时，直接落实相应决定；明确批准当前版本后继续推进。
- 自动模式由你验收方案并推进，refine 完成后直接执行授权范围内的下一阶段。
- awaiting_user 表示等待用户回答，此时可读取材料、整理问题和落实用户要求的 Markdown 修改；用户提交明确答案后再恢复派单。
- awaiting_results、failed 或 interrupted 时，核对实际状态和原因，向用户说明后续处理。

## 7. 角色记录与研究资源
- role_cli 用于只读查询 list、status、history，当前项目已绑定，无需填写 --project。例如：history --role paper_writer --channel discussion。
- history 按 next_offset/next_char_offset 续读；status 可查看摘要前的 archive 文件。
- 角色窗口对话保存为讨论记录。用户提到相关讨论时，先读取对应 discussion，再将本次确认的结论写入文件或正式派单。
- 数据检索和样本理解交给 data_search 阶段，主控按需定向核对；文献阶段聚焦文献。
- 资源库使用 battery_resources：search 检索说明与登记路径，read 读取 resource_id，inspect 检查真实数据文件结构。
- 用户提到的数据集先在资源库查证，结合领域、来源、字段和路径识别；多个版本按任务匹配度选择并记录依据。
- 使用 inspect_compute 核对算力。预测实验的基础代码由 experiment_bridge 阶段通过模板工具准备；主控提供任务和已核实的数据路径，已有可用实现时沿用。

## 8. 面向用户的表达
- 耗时操作前和获得关键结果后，用一两句说明当前目标、已核实发现和下一步。
- 公开说明围绕可核查的进展和证据，不输出内部推理链。
- 最终用中文简要说明完成内容、证据位置、尚缺内容和下一步，区分已执行结果与后续计划。
""".format(workflow_order=" → ".join(WORKFLOW_NODE_NAMES), **ROLE_DESCRIPTIONS)


@dataclass
class CoordinatorTurn:
    """Mutable state of one user request; project workflow state stays on disk."""
    status: str = 'completed'
    error: str | None = None
    stop_reason: str | None = None
    assignments: int = 0
    archive_due: bool = False
    approval_snapshot: dict | None = None
    checkpoint_revision: str | None = None
    approve_revision: bool = False
    edited_paths: set[str] = field(default_factory=set)
    read_ledger: dict = field(default_factory=dict)
    invalid_batch: bool = False

    def accept_result(self, status, error=None):
        self.status, self.error = status, error
        # Definite outcomes may be inspected and retried as fresh assignments.
        self.stop_reason = (None if status in {'completed', 'failed', 'interrupted', 'awaiting_results'}
                            else f'Worker returned {status}: {error or ""}')

    def pause(self, reason, *, status='awaiting_user'):
        self.status, self.stop_reason = status, reason

    def fail(self, error):
        self.status, self.error = 'failed', str(error)


class CoordinatorAgent(AgentLoop):
    auto_continue_iterations = True

    def __init__(self, project, *, model=None, model_client=None, max_iterations=None,
                 max_assignments=None, worker_max_iterations=300, timeout=600,
                 mode=None, on_event=None, context_max_tokens=CONTEXT_MAX_TOKENS,
                 execution_guard=None, stage_task=None,
                 workflow_options=None, checkpoint_revision=None, stop_after=None, first_node=None, approve_revision=False, project_goal=None,
                 wait_for_user=False):
        self.workflow_options = workflow_options or {}
        self.turn = CoordinatorTurn(checkpoint_revision=checkpoint_revision,
                                    approve_revision=approve_revision)
        self.stop_after = stop_after
        self.first_node = first_node
        self.project_goal = project_goal
        self.wait_for_user = wait_for_user
        self.project = Path(project).resolve()
        from batteryscientist.project_config import resolve_mode
        mode = resolve_mode(self.project, mode)
        repo = Path(__file__).resolve().parents[2]
        load_env_file(repo / ".env")
        load_env_file(repo / ".env.example")
        self.worker_model = model
        self.worker_max_iterations = worker_max_iterations
        self.timeout, self.mode = timeout, mode
        self.max_assignments = max_assignments if max_assignments is not None else (
            len(WORKFLOW_NODE_NAMES) + 2 if mode == 'auto' else 6)
        if max_iterations is None:
            max_iterations = 4 * len(WORKFLOW_NODE_NAMES) + 8 if mode == 'auto' else 24
        if mode not in {"auto", "expert"} or self.max_assignments < 1 or context_max_tokens < 1000:
            raise ValueError("Invalid coordinator mode or budget")
        self.context_max_tokens = context_max_tokens
        self.on_event = on_event
        self.execution_guard = execution_guard
        self.stage_task = StageAssignment.model_validate(stage_task) if stage_task else None
        self._active = None
        self.research_memory_skill = (repo / "batteryscientist/skills/research-memory/SKILL.md").read_text(encoding="utf-8")
        super().__init__(model_client, workspace=self.project, tools=self._registered_tools(),
                         max_iterations=max_iterations)
        if (self.project / '电池资源库').is_dir():
            self.knowledge_roots = (self.project / '电池资源库',)

    def _registered_tools(self):
        """Select exposed capabilities; AgentLoop derives parameter schemas from handlers."""
        return tuple(spec for spec in BUILTIN_TOOLS if spec.name in {
            'battery_resources', 'find',
        }) + (
            ToolSpec("role_cli", "只读查询角色：仅支持list/status/history。参数command是命令文本，无需--project。执行任务使用delegate_stage，Wiki整理使用curate_wiki。", self._role_cli, True),
            ToolSpec("get_project_context", "读取最新项目状态、产物目录和阶段任务。", self._context, True),
            ToolSpec("inspect_compute", "只读检查当前执行环境的CPU、CUDA/MPS可用性和项目计算限制。", self._inspect_compute, True),
            ToolSpec("set_research_goal", "依据用户明确的研究需求记录项目总目标goal；可整合此前的澄清答案。仅设置目标，不派单、不增加用户确认。", self._set_research_goal),
            ToolSpec("read_project_file", "读取项目中的文本产物。offset和limit均以字符计数，不是行数；默认读取12000字符，单次最多20000字符。通常只需提供真实path；需要续读时使用返回的next_offset。普通read工具才按行读取。", self._read, True),
            ToolSpec("edit_project_markdown", "主控直接落实用户要求：先读文件，再以path、old_text、new_text精确替换一处Markdown。node为所属工作流节点。修改不会批准或派单。", self._edit_markdown),
            ToolSpec("review_checkpoint", "主控理解待审产物后更新面向用户的summary、questions及可选decision_options；沿用当前检查点，不批准、不派单。", self._review_checkpoint),
            ToolSpec("approve_checkpoint", "仅落实用户对当前待审版本的明确批准，沿用工作流校验；无明确批准或版本变化时拒绝。", self._approve_checkpoint),
            ToolSpec("curate_wiki", "派单给正式wiki_agent角色，阅读、生成、独立审核、修订并发布共享Wiki。单篇PDF用project_evidence=false；研究收尾用true。无需先设置研究目标。", self._curate_wiki),
            ToolSpec("delegate_stage", "统一串行派单：assignment包含node、role、instruction、full_stage。完整研究阶段设full_stage=true，普通修改或检查设false（默认），按本次指令工作，不套整阶段验收。", self._delegate),
            ToolSpec("revise_checkpoint", "复杂研究修订才发回原角色；普通Markdown修改用edit_project_markdown。生成新版后等待审批，node必须匹配待审节点。", self._revise_checkpoint),
            ToolSpec("request_user_decision", "提问用户；context_paths 填写本问题依赖的项目内文件相对路径，文件变化会使回答失效。自动模式仅限已查证且无法自行解决的必要输入阻塞，必须填写blocking_reason。", self._ask),
        )

    def _tool_schemas(self):
        schemas = super()._tool_schemas()
        for schema in schemas:
            name = schema["function"]["name"]
            if name == "delegate_stage":
                key, model = "assignment", StageAssignment
                schema["function"]["parameters"] = {
                    "type": "object", "properties": {key: model.model_json_schema()},
                    "required": [key], "additionalProperties": False}
            elif name == 'review_checkpoint':
                schema['function']['parameters']['properties']['decision_options'] = {
                    'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}}}
            elif name == 'read_project_file':
                properties = schema['function']['parameters']['properties']
                properties['offset']['description'] = '跳过的字符数，从0开始；续读请使用返回的next_offset，不是行号。'
                properties['limit']['description'] = '读取的字符数，不是行数；默认12000，最多20000，通常省略即可。'
        return schemas

    def _context(self, ctx) -> ToolResult:
        value = project_context({"path": str(self.project), "name": self.project.name})
        value["phase_roles"] = {node: stage.role for node, stage in BY_NODE.items()}
        value["decision"] = self._load_decision()
        from batteryscientist.agent.runtime.questions import UserQuestions
        value['pending_questions'] = UserQuestions(self.project).pending()
        value["research_goal"] = self.project_goal or value["research_goal"]
        value['mode'] = self.mode
        value['battery_resource_roots'] = [str(path) for path in self.knowledge_roots]
        value['compute_policy'] = load_compute_config(self.project)
        value['execution_policy'] = load_execution_config(self.project)
        memory = read_json(self.project / '.batteryscientist/research-memory.json', {})
        value['research_memory'] = {key: memory.get(key) for key in
                                    ('status', 'wiki_path', 'idea_path', 'experiment_paths', 'error', 'updated_at')}
        return ToolResult(json.dumps(value, ensure_ascii=False))

    def _inspect_compute(self, ctx) -> ToolResult:
        from batteryscientist.tools.experiment_resources import inspect_compute
        return inspect_compute(ToolContext(self.project))

    def _set_research_goal(self, ctx, goal: str) -> ToolResult:
        goal = goal.strip()
        if not goal:
            raise ValueError('研究目标不能为空')
        saved_goal = read_json(self.project / 'workflow-state.json', {}).get('project_goal')
        if saved_goal and saved_goal != goal:
            raise ValueError('已有工作流的研究目标不能由对话替换；请通过现有方案修订流程调整研究内容')
        session, data = self._active.session, self._active.data
        self.project_goal = data['project_goal'] = goal
        session.save(data)
        return ToolResult(json.dumps({'project_goal': goal}, ensure_ascii=False))


    def _curate_wiki(self, ctx, paper_path: str = "paper/main.pdf", project_evidence: bool = False,
                     source_paths: list[str] | None = None, instruction: str = "",
                     force: bool = False) -> ToolResult:
        if self.stage_task or self.stop_after not in (None, 'improvement'):
            raise ValueError('A scoped stage/revision request does not include Wiki publication')
        self._execution_gate(count_assignment=not project_evidence)
        ToolContext(self.project).resolve_read(paper_path)
        task_instruction = instruction.strip() or (
            '将最终论文与相关项目证据整理入共享Wiki，保留重要负结果与适用边界。' if project_evidence else
            '完整阅读指定论文，整理Paper、Idea和重要Experiment，经独立审核后发布到共享Wiki。')
        self._progress('archive_start', role='wiki_agent', paper_path=paper_path)
        if project_evidence:
            self.turn.archive_due = False
        try:
            state = execute_stage(
                self.project, 'wiki', task_instruction, kind='wiki', role='wiki_agent',
                project_goal=self.project_goal or '', paper_path=paper_path,
                project_evidence=project_evidence, source_paths=source_paths, force=force,
                model=self.worker_model, timeout=self.timeout, max_iterations=self.worker_max_iterations,
                interaction_mode=self.mode, on_event=self._worker_event)
            result = state.wiki_result or {'status': state.status, 'error': state.error}
            if not project_evidence:
                self.turn.accept_result(state.status, state.error)
            self._progress('archive_end', **result)
            return ToolResult(json.dumps(result, ensure_ascii=False), state.status != 'completed')
        except Exception as exc:
            result = {'status': 'failed', 'role': 'wiki_agent', 'error': str(exc),
                      'message': 'Research artifacts are preserved. Retry curate_wiki separately.'}
            if not project_evidence:
                self.turn.fail(exc)
                self.turn.stop_reason = str(exc)
            self._progress('archive_end', **result)
            return ToolResult(json.dumps(result, ensure_ascii=False), True)

    def _complete_research(self):
        if self.turn.archive_due:
            raise ArtifactValidationError('完整研究已完成，请调用 curate_wiki(project_evidence=true) 沉淀成果后交付总结。')

    def _role_cli(self, ctx, command: str) -> ToolResult:
        from .role_cli import run
        value = run(command, project=self.project, read_only=True)
        return value if isinstance(value, ToolResult) else ToolResult(json.dumps(value, ensure_ascii=False))

    def _read(self, ctx, path: str, offset: int = 0, limit: int = 12000) -> ToolResult:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError("offset must be a nonnegative integer; limit must be a positive integer")
        limit = min(limit, 20000)
        relative = Path(path)
        target = (self.project / relative).resolve()
        if (relative.is_absolute() or not target.is_relative_to(self.project)
                or any(part.startswith(".") or part in {"audit", "backup", "backups"} for part in relative.parts)
                or any(part.startswith(".") for part in target.relative_to(self.project).parts)
                or target.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".tex", ".bib", ".py"}
                or not target.is_file() or target.stat().st_size > 512000):
            raise ValueError("Only bounded project text artifacts may be read")
        content = target.read_bytes()
        text = content.decode('utf-8')
        digest = hashlib.sha256(content).hexdigest()
        key = relative.as_posix()
        ledger = self.turn.read_ledger.get(key)
        end = min(len(text), offset + limit)
        if ledger and ledger["sha256"] == digest:
            covered = any(start <= offset and finish >= end for start, finish in ledger["ranges"])
            if covered:
                return ToolResult(json.dumps({"path": path, "cached": True,
                                              "sha256": digest,
                                              "message": "This unchanged range was already read in this coordinator invocation; use the prior tool result instead.",
                                              "next_offset": None if end >= len(text) else end},
                                             ensure_ascii=False))
        if not ledger or ledger["sha256"] != digest:
            ledger = {"sha256": digest, "ranges": []}
            self.turn.read_ledger[key] = ledger
        ledger["ranges"].append((offset, end))
        return ToolResult(json.dumps({"path": path, "content": text[offset:offset + limit],
                                     "sha256": digest,
                                     "next_offset": offset + limit if len(text) > offset + limit else None},
                                    ensure_ascii=False))

    def _require_current_read(self, path):
        target = (self.project / path).resolve()
        ledger = self.turn.read_ledger.get(path)
        if (not target.is_relative_to(self.project) or not target.is_file()
                or not ledger or ledger['sha256'] != hashlib.sha256(target.read_bytes()).hexdigest()):
            raise ValueError(f'先读取当前文件再操作：{path}')
        return target

    def _require_answered_questions(self):
        from batteryscientist.agent.runtime.questions import UserQuestions
        if UserQuestions(self.project).pending():
            self.turn.status = 'awaiting_user'
            raise ValueError('请先在网页问题卡片或用 /answer 回答待处理问题；查看进度不会解除等待')

    @contextmanager
    def _project_update(self, *, require_answers=False):
        """Serialize main-owned writes and exclude unfinished role execution."""
        from batteryscientist.agent.runtime.execution import project_write_lock
        from batteryscientist.agent.runtime.herdr import ensure_no_pending
        with project_write_lock(self.project):
            ensure_no_pending(self.project)
            if require_answers:
                self._require_answered_questions()
            yield

    def _edit_markdown(self, ctx, node: str, path: str, old_text: str, new_text: str) -> ToolResult:
        from .workflow_cli import refresh_revision_checkpoint
        from batteryscientist.proposal_review import CANVAS, RECORD, PROPOSAL, write_group
        relative = Path(path)
        if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or any(part.startswith('.') or part in {'audit', 'backup', 'backups'} for part in relative.parts)
                or relative.parts[0] in {'results', 'experiment-runs', 'papers'}
                or relative.name.startswith(('workflow-', 'experiment-queue', 'project-config'))):
            raise ValueError('不允许修改此路径：' + path)
        saved = read_json(self.project / 'workflow-state.json', {})
        owners = [n for n, refs in saved.get('artifacts', {}).items()
                  if any(ref['path'] == path for ref in refs)]
        if owners and node not in owners:
            raise ValueError('node必须是此产物所属的阶段')
        if self.stop_after and WORKFLOW_NODE_NAMES.index(node) > WORKFLOW_NODE_NAMES.index(self.stop_after):
            raise ValueError('修改超出本次用户授权的最后阶段')
        if Path(path).suffix.lower() != '.md':
            raise ValueError('主控直接编辑仅支持Markdown；代码或实验修改请委派角色')
        if self.stage_task and self.stage_task.node != node:
            raise ValueError('修改超出本次用户的精确任务范围')
        with self._project_update(require_answers=True):
            target = self._require_current_read(path)
            before = target.read_bytes().decode('utf-8')
            if not old_text or before.count(old_text) != 1:
                raise ValueError('old_text必须准确匹配当前文件中的唯一一处内容')
            after = before.replace(old_text, new_text, 1)
            if not after.strip() or len(after.encode('utf-8')) > 512000:
                raise ValueError('Markdown不能为空或超过512KB')
            if after == before:
                return ToolResult(json.dumps({'path': path, 'changed': False}))
            changes = {path: after.encode('utf-8')}
            if path == PROPOSAL:
                # The MD becomes the current source. Preserve old canvas edits for
                # traceability, rather than leaving a stale candidate able to replace it.
                for name in (CANVAS, RECORD):
                    draft = self.project / name
                    if draft.exists():
                        self._require_current_read(name)
                        changes[f'refine-logs/review-history/main-{uuid4().hex}-{draft.name}'] = draft.read_bytes()
            write_group(self.project, changes)
            if path == PROPOSAL:
                for name in (CANVAS, RECORD):
                    (self.project / name).unlink(missing_ok=True)
            self.turn.edited_paths.add(path)
            pending = refresh_revision_checkpoint(self.project, node, self.project_goal, files=[path])
            self.turn.approval_snapshot = None  # An answer approving the old text never approves this edit.
            if pending:
                self.turn.pause('Markdown已修订，等待用户审阅当前版本。')
            result = {'path': path, 'node': node, 'changed': True, 'status': self.turn.status,
                      'sha256': hashlib.sha256(after.encode('utf-8')).hexdigest(),
                      'message': '主控已直接更新MD；修改意见未被当作批准。'}
            self._progress('artifact_edited', **result)
            return ToolResult(json.dumps(result, ensure_ascii=False))

    def _review_checkpoint(self, ctx, summary: str, questions: list[str] | None = None,
                           decision_options: list[list[str]] | None = None) -> ToolResult:
        questions = questions or []
        if (not summary.strip() or any(not q.strip() for q in questions)
                or decision_options is not None and len(decision_options) != len(questions)):
            raise ValueError('请提供简短摘要及与实际取舍相关的问题，选项应逐题对应')
        with self._project_update():
            state = read_json(self.project / 'workflow-state.json', {})
            checkpoint = state.get('expert_checkpoint') or {}
            if state.get('status') != 'awaiting_user' or checkpoint.get('status') != 'pending':
                raise ValueError('没有待审检查点')
            checkpoint.update(summary=summary, questions=questions)
            checkpoint.pop('decision_options', None)
            if decision_options is not None:
                checkpoint['decision_options'] = decision_options
            _write_json(self.project / 'workflow-state.json', state)
            self.turn.status = 'awaiting_user'
            self._progress('coordinator_decision', checkpoint=checkpoint)
            return ToolResult(json.dumps(checkpoint, ensure_ascii=False))

    def _approve_checkpoint(self, ctx) -> ToolResult:
        from .workflow_cli import approve_checkpoint
        if self.turn.approval_snapshot is None:
            raise ValueError('需要用户明确批准当前版本；修改、讨论或“继续”不等于批准')
        self._require_answered_questions()
        state, boundary = approve_checkpoint(self.project, self.turn.approval_snapshot)
        self.turn.approval_snapshot = None
        self.turn.accept_result('completed')
        self.stop_after = self.stop_after or boundary
        if (state.status == "completed" and state.current_node == "improvement"
                and not self.stage_task):
            self.turn.archive_due = True
        self._progress('checkpoint_approved', node=state.expert_checkpoint['node'])
        return ToolResult(json.dumps({'approved': True, 'next_node': state.current_node,
                                     'status': state.status, 'stop_after': self.stop_after}, ensure_ascii=False))

    def _execution_gate(self, *, checkpoint_node=None, revision=False, count_assignment=True):
        state = read_json(self.project / "workflow-state.json", {})
        if self.turn.stop_reason and not (revision and state.get('status') == 'awaiting_user'
                                         and self.turn.status == 'awaiting_user'):
            raise ValueError(f"Dispatch stopped: {self.turn.stop_reason}")
        self._require_answered_questions()
        revising_upstream = (revision and checkpoint_node in WORKFLOW_NODE_NAMES
                             and state.get('current_node') in WORKFLOW_NODE_NAMES
                             and WORKFLOW_NODE_NAMES.index(checkpoint_node) <= WORKFLOW_NODE_NAMES.index(state['current_node']))
        if state.get("status") == "awaiting_user" and not (
                self.turn.checkpoint_revision and checkpoint_node == state.get("current_node") or revising_upstream):
            self.turn.status = "awaiting_user"
            raise ValueError("User checkpoint must be approved through the existing review endpoint")
        if not count_assignment:
            return
        if self.turn.assignments >= self.max_assignments:
            self.turn.pause("Assignment budget exhausted")
            raise ValueError(self.turn.stop_reason)
        self.turn.assignments += 1

    def _delegate(self, ctx, assignment: dict) -> ToolResult:
        task = StageAssignment.model_validate(assignment)
        if self.stage_task and (
                task.node != self.stage_task.node or task.role != self.stage_task.role
                or task.full_stage != self.stage_task.full_stage):
            raise ValueError("Execution must match the user's explicit assignment")
        if self.stage_task and task.instruction != self.stage_task.instruction:
            task = task.model_copy(update={"instruction": (
                "原始授权任务（必须遵守）：\n" + self.stage_task.instruction
                + "\n\n主控补充的阶段执行要求（不得改变原始授权范围）：\n" + task.instruction
            )})
        if self.first_node and self.turn.assignments == 0 and task.node != self.first_node:
            raise ValueError("First assignment must match the requested starting node")
        if self.stop_after and WORKFLOW_NODE_NAMES.index(task.node) > WORKFLOW_NODE_NAMES.index(self.stop_after):
            raise ValueError("Assignment exceeds the user's stop boundary")
        if task.full_stage and not self.project_goal:
            return ToolResult('研究目标尚未确定；根据用户的研究需求及澄清答案调用set_research_goal后再派单。', True)
        self._execution_gate(checkpoint_node=task.node, revision=not task.full_stage or bool(self.turn.checkpoint_revision))
        if task.full_stage and self.execution_guard is not None:
            self.execution_guard(task.node)
        self._progress("assignment_start", assignment=task.model_dump())
        state = execute_stage(self.project, task.node, task.instruction,
                             revise=self.turn.checkpoint_revision, project_goal=self.project_goal or "",
                             full_stage=task.full_stage,
                             role=task.role, model=self.worker_model,
                             mode=None if self.turn.checkpoint_revision else self.mode,
                             timeout=self.timeout, max_iterations=self.worker_max_iterations,
                             on_event=self._worker_event, workflow_stop_after=self.stop_after,
                             approve_revision=self.turn.approve_revision,
                             **self.workflow_options)
        self.turn.checkpoint_revision = None
        checkpoint = getattr(state, "expert_checkpoint", None)
        result = {"status": state.status, "error": state.error,
                  "task_id": state.run_id if task.full_stage else state.task_id,
                  "final": (state.node_results.get(task.node, {}).get("final", "")
                            if task.full_stage else state.final),
                  "artifacts": ([ref["path"] for ref in state.artifacts.get(task.node, [])]
                                if task.full_stage else state.files),
                  "checkpoint": {key: checkpoint[key] for key in
                                 ("node", "status", "summary", "questions", "decision_options")
                                 if key in checkpoint} if checkpoint else None}
        self.turn.accept_result(state.status, state.error)
        if (task.full_stage and task.node == 'improvement' and state.status == 'completed' and
                not self.stage_task and self.stop_after in (None, 'improvement')):
            self.turn.archive_due = True
            result['research_memory'] = 'Call curate_wiki(project_evidence=true) before summarizing.'
        if state.status == 'completed' and (self.stage_task or self.stop_after == task.node and not self.turn.archive_due):
            self.turn.stop_reason = "The explicitly assigned stage has ended; summarize and stop."
        self._progress("assignment_end", node=task.node, role=task.role, **result)
        return ToolResult(json.dumps(result, ensure_ascii=False), state.status in {"failed", "interrupted"})

    def _revise_checkpoint(self, ctx, node: str, instruction: str) -> ToolResult:
        state = read_json(self.project / 'workflow-state.json', {})
        checkpoint = state.get('expert_checkpoint') or {}
        if (state.get('status') != 'awaiting_user' or checkpoint.get('status') != 'pending'
                or checkpoint.get('node') != node or state.get('current_node') != node):
            raise ValueError('修订必须对应当前待审批节点')
        if self.stage_task:
            raise ValueError('本次已有精确任务，不可改为整阶段修订')
        self.turn.checkpoint_revision = instruction
        # The existing revision path preserves checkpoint guards and regenerates
        # the role's review summary; never autoapprove this newly revised version.
        self.turn.approve_revision = False
        from .workflow_cli import checkpoint_author
        result = self._delegate(ctx, {'node': node, 'role': checkpoint_author(self.project, node),
                                      'instruction': instruction, 'full_stage': True})
        self.turn.stop_reason = '新版已交付，请用户审阅并批准后再继续。'
        return result

    def _ask(self, ctx, question: str, options: list[str] | None = None,
             blocking_reason: str = '', context_paths: list[str] | None = None) -> ToolResult:
        if self.mode == 'auto' and not blocking_reason.strip():
            return ToolResult(AUTO_QUESTION_REQUIREMENT, True)
        from batteryscientist.agent.runtime.questions import UserQuestions
        from .workflow_cli import checkpoint_snapshot, is_approval
        snapshot = checkpoint_snapshot(self.project)
        store = UserQuestions(self.project)
        value = store.create(question, role='main', task_id=self._active.record['id'],
                             phase='coordinator', options=options, live=self.wait_for_user,
                             blocking_reason=blocking_reason.strip(), context_paths=context_paths)
        if self.wait_for_user:
            session, data, task = self._active.session, self._active.data, self._active.record
            task.update(status='awaiting_user', question_id=value['id'])
            session.save(data)
            answer = store.wait(value, self._progress)
            self.turn.approval_snapshot = snapshot if is_approval(answer) else None
            task.update(status='running')
            session.save(data)
            return ToolResult(json.dumps({'question_id': value['id'], 'answer': answer}, ensure_ascii=False))
        self.turn.pause(question)
        self._progress("user_question", question=value)
        return ToolResult("Question recorded. Stop dispatching and present this question to the user.")

    def _load_decision(self):
        from batteryscientist.agent.runtime.questions import UserQuestions
        return UserQuestions(self.project).latest(role='main')

    async def _invoke(self, spec, arguments):
        # Stage runtimes own asyncio.run(); keep their loops out of this event loop.
        critical = self._critical_operation(spec.name)
        if critical and self.turn.invalid_batch:
            self.turn.stop_reason = "Multiple critical operations in one response"
            self.turn.fail(self.turn.stop_reason)
            raise ValueError("Submit at most one critical operation per model response")
        try:
            ctx = ToolContext(self.project, self.skill_dirs, self.knowledge_roots)
            return await asyncio.to_thread(spec.handler, ctx, **arguments)
        except asyncio.CancelledError:
            # asyncio's first SIGINT cancels the coroutine. Release any question
            # wait in the executor before asyncio.run joins its worker threads.
            import os
            from batteryscientist.agent.runtime.questions import UserQuestions
            UserQuestions(self.project).interrupt_owned(os.getpid())
            raise
        except Exception as exc:
            if critical and spec.name != 'edit_project_markdown':
                if self.turn.status == "completed":
                    self.turn.fail(exc)
            raise

    def _worker_event(self, record):
        if self.on_event:
            self.on_event(record)

    @staticmethod
    def _critical_operation(name):
        return name in {'delegate_stage', 'revise_checkpoint', 'request_user_decision',
                        'curate_wiki', 'edit_project_markdown', 'review_checkpoint', 'approve_checkpoint'}

    def _prepare_tool_batch(self, calls):
        self.turn.invalid_batch = sum(self._critical_operation(call.name) for call in calls) > 1

    def _progress(self, kind, **data):
        if self.on_event:
            identifiers = {}
            if self._active:
                identifiers = {"session_id": self._active.data["session_id"],
                               "task_id": self._active.record["id"]}
            self.on_event({"kind": kind, "time": now(), "role": "main", **identifiers, **data})

    def _save_conversation(self, state):
        if not self._active:
            return
        session, data, task = self._active.session, self._active.data, self._active.record
        session.save_conversation(data, task, state)

    def _working_context_compacted(self, phase, iteration):
        # A summary may replace the earlier read result. Allow evidence to be
        # fetched again rather than directing the model to invisible history.
        self.turn.read_ledger = {}
        super()._working_context_compacted(phase, iteration)

    def dispatch(self, task: StageAssignment) -> ToolResult:
        """Execute an explicit CLI assignment without another main-model turn."""
        with RoleSession(self.project, 'main').locked():
            saved = read_json(self.project / 'workflow-state.json', {})
            main = RoleSession(self.project, 'main').load()
            self.project_goal = saved.get('project_goal') or main.get('project_goal') or task.instruction
            return self._delegate(None, task)

    def go(self, prompt, *, archive_completed=False):
        if not prompt.strip():
            raise ValueError("Main assignment must not be empty")
        if self.model is None:
            self.model = ModelClient(model=self.worker_model, timeout=self.timeout)
        self.project.mkdir(parents=True, exist_ok=True)
        saved = read_json(self.project / "workflow-state.json", {})
        from .workflow_cli import checkpoint_snapshot, is_approval
        self.turn.approval_snapshot = checkpoint_snapshot(self.project) if is_approval(prompt) else None
        self.project_goal = saved.get("project_goal") or self.project_goal
        session = RoleSession(self.project, "main")
        def prepare_session(data, task):
            nonlocal prompt
            self.project_goal = self.project_goal or data.get('project_goal') or ''
            data['project_goal'] = self.project_goal
            if prompt.startswith('/answer '):
                from batteryscientist.agent.runtime.questions import answer_command
                answered = answer_command(self.project, prompt)
                self.turn.approval_snapshot = checkpoint_snapshot(self.project) if is_approval(answered['answer']) else None
                prompt = f"用户已正式回答问题 [{answered['id']}]：{answered['answer']}"
            task['instruction'] = prompt

        task = {"id": uuid4().hex, "instruction": prompt}
        self.last_state = None
        try:
            with session.task(task, model=self.model, max_tokens=self.context_max_tokens,
                              get_state=lambda: self.last_state, before_start=prepare_session,
                              on_compacting=lambda: self._context_compaction_event(True),
                              on_compacted=lambda: self._context_compaction_event(False)) as active:
                self._active = active
                data, history = active.data, active.history
                recovered_results = active.recovered_results
                self.turn = CoordinatorTurn(
                    status='awaiting_user' if saved.get('status') == 'awaiting_user' else 'completed',
                    archive_due=archive_completed, approval_snapshot=self.turn.approval_snapshot,
                    checkpoint_revision=self.turn.checkpoint_revision,
                    approve_revision=self.turn.approve_revision)
                decision = self._load_decision()
                if decision.get('status') == 'awaiting_user':
                    self.turn.status = 'awaiting_user'
                context = json.dumps({
                    "project_path": str(self.project), "research_goal": self.project_goal,
                    "mode": self.mode, "workflow_state_path": "workflow-state.json",
                    "project_config_path": "project-config.json",
                }, ensure_ascii=False)
                context += f"\n本次用户任务执行预算：最多 {self.max_assignments} 次串行派单；该额度跨本次所有模型响应计算，并非每次只允许一个阶段。"
                if recovered_results:
                    context += ("\n上次中断有工具结果未写入对话。先核对当前状态、实际文件和原任务回执，"
                                "复用已完成工作，再按本次授权继续。派单会核对未结束的执行任务。")
                if self.first_node:
                    context += f"\n本次从 {self.first_node} 阶段开始，逐节点分配并验收。"
                if self.stop_after:
                    context += f"\n用户授权的最后阶段为 {self.stop_after}，完成后停止，不得派发后续节点。"
                if self.stage_task:
                    context += "\n本次唯一授权的任务（角色、阶段能力、完整阶段标记及任务范围不得替换或扩展；可补充细节，完成后总结并停止）：\n" + json.dumps(
                        {"delegate_stage": self.stage_task.model_dump()}, ensure_ascii=False)
                request = ("遵循上述项目总目标中本次用户请求的执行范围。先判断是讨论、查询还是执行；"
                           "仅讨论或查询时不要派单；需要执行时逐项验收并推进。"
                           if prompt == self.project_goal else prompt)
                if self.stage_task and prompt == self.stage_task.instruction:
                    request = "执行本次唯一授权的阶段任务，验收并总结后停止。"
                result = self.run(request, system_prompt=SYSTEM + "\n" + self.research_memory_skill + "\n当前项目：\n" + context
                                  + interaction_instructions(self.mode),
                                  phase="coordinator", history=history, completion_check=self._complete_research)
                if (self.stage_task and not self.turn.edited_paths) and self.turn.assignments == 0:
                    raise RuntimeError("Main agent did not execute the explicit assignment")
                if not result.final.strip():
                    raise RuntimeError("Main agent returned an empty summary")
                task.update(status=self.turn.status, final=result.final, error=self.turn.error)
                return SimpleNamespace(status=self.turn.status, error=self.turn.error,
                                       final=result.final, task_id=task["id"], session_id=data["session_id"])
        finally:
            self._active = None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run the persistent research coordinator")
    parser.add_argument("prompt")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--mode", choices=["auto", "expert"], default="auto")
    parser.add_argument("--max-assignments", type=int, default=None)
    parser.add_argument("--max-tool-rounds", type=int, default=300)
    args = parser.parse_args()

    def guard(node):
        if node == "experiment_bridge":
            from batteryscientist.tools.experiments.executor import check_runtime
            check_runtime()

    result = CoordinatorAgent(args.project, model=args.model, mode=args.mode,
                              max_assignments=args.max_assignments,
                              worker_max_iterations=args.max_tool_rounds,
                              execution_guard=guard).go(args.prompt)
    print(result.final)
    if result.status in {"failed", "interrupted"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
