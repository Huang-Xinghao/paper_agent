# 冗余字段、函数与旧兼容审查（A 及部分 B 已完成）

审查日期：2026-11-10。基于当前工作区，包含未提交及未跟踪的源码。初次审查仅新增清单；随后用户以“a的先删”批准 A1–A8，现已完成对应源码、调用方和测试清理。随后用户授权继续删除判断可清理的 B 项，已实施 B1/B2/B3/B4/B6/B7；复核后保留 B5/B8/B9/B10。C 项未改动。

以下审查位置与行号保留初审记录，删除后行号可能变化；当前处理状态以各项标注和末尾实施记录为准。

检查范围：`paper_agent/`、`scripts/`、`mcp-servers/image-gen/server.py` 的 76 个 Python 文件、17,138 行源码，以及测试、技能、入口脚本、配置示例和相关文档。使用 AST 检查定义、参数、导入、属性读写和重复函数体，再核对文本引用、工具注册、阶段白名单、CLI 与序列化路径。没有把装饰器注册、回调、继承属性、公开响应字段简单视为“无调用”。

限制：结论针对本仓库；独立前端只核对了 message_prompt 的消费路径，未完整审查前端、仓库外 Python 调用方和所有部署项目的历史状态。没有启动研究流程、调用外部模型或修改已有研究数据。初次审查基线为 **170 passed in 2.11s**；A1–A8 实施后，受影响测试为 **283 passed in 5.46s**，详见末尾验证记录。

## A：已批准并完成的清理项

这些项目在当前仓库中没有业务消费者，或存在明确、可保持行为的简化方式。公开 Python 方法仍可能有仓库外使用者，不能据此承诺对外零影响。

| 编号 | 位置 | 建议删除或简化 | 证据与联动 |
| --- | --- | --- | --- |
| A1 | `paper_agent/agent/wiki_quality.py:568` | 删除 `refresh_connections()` | 生产源码、脚本、测试均没有调用。当前关系流程调用 `discover_relations()`、`write_relations()`；删除该孤立函数不等于删除图关系功能。 |
| A2 | `paper_agent/agent/wiki_quality.py:560` | 删除 `render_sections()` | 只剩测试引用，函数仅返回 `dict(draft["sections"])`，`document/report` 两个参数完全未用。当前 WikiAgent 直接使用分析结果的 sections。测试应转而检查实际发布路径；保留来源、审核、纠错等有效断言。 |
| A3 | `paper_agent/tools/wiki.py:188` | 删除 `browse_wiki()` | 没有业务调用，未注册为模型工具。现有测试通过 `_invoke_internal()` 人工调用它；当前 `search_wiki()` 使用自己的相关性排序和卡片生成。需删除只验证旧浏览接口的测试，保留统一查询工具的分页、摘录、来源和范围测试。 |
| A4 | `paper_agent/tools/core.py:60` | 删除 `ToolContext.resolve_knowledge()` | 只有定义，没有调用或测试。当前读取使用 `resolve_read()` → `resolve_data_read()`。保留 `knowledge_roots` 及实际路径校验。 |
| A5 | `paper_agent/agent/role_terminal.py:64`、`:307`、`:382` | 删除 `self.operation` 字段及两处接收任务返回值的赋值 | 全仓只有写入，没有读取、等待或取消。保留两次 `self.app.create_background_task(...)` 调用；取消仍走现有 cancel 文件与进程回执。 |
| A6 | 见下表 | 删除 6 个未使用的导入名 | AST 与全仓引用复核均无消费；保留导入语句中其他实际使用的名称及公开重导出。 |
| A7 | `paper_agent/tools/wiki.py:32`；`paper_agent/agent/wiki_citations.py:156` | 删除 `_wiki_root(ctx)` 的 `ctx` 参数；删除 `save_references(root, ...)` 的 `root` 参数 | 两个参数均未被函数体使用。更新 wiki.py、wiki_hybrid.py、WikiAgent 与 citation 测试中的调用；这不是移除 ToolContext 或引用持久化功能。`save_references` 的 Python 调用签名会改变。 |
| A8 | `paper_agent/tools/research_wiki.py:1038`、`:1242` | 合并两份局部 `_norm()`，删除重复实现 | AST 比较确认函数体相同，均规范化带命名空间的节点 ID / gap ID。提取一个模块内辅助函数，更新 Claim 和 Idea 两条调用路径。不要顺带合并两份行为不同的 `_warn_if_dangling()`。 |

A6 的完整范围：

| 文件 | 删除的导入名 |
| --- | --- |
| `paper_agent/agent/loop.py:12` | `time` |
| `paper_agent/agent/loop.py:24` | `ModelResponse` |
| `paper_agent/agent/model_client.py:9` | `dataclasses.field` |
| `paper_agent/agent/role_terminal.py:6` | `os` |
| `paper_agent/tools/figure_grouping.py:167` | `PIL.Image`，保留 `ImageDraw` |
| `paper_agent/agent/wiki_research.py:13` | `RESEARCH_POLICY as POLICY`，保留实际使用的 `RESEARCH_PROMPT as SYSTEM` 等导入 |

`loop.py` 的 `ToolCall` 虽然本文件不直接执行它，但位于 `__all__` 且 `tests/test_paper_agent.py:3` 从该处导入，不属于 A6。

## B：复核与实施结果

用户授权：“b你觉得能删的也删了吧”。按实际消费者、当前调用链及历史数据依赖复核，结果如下：

| 编号 | 状态 | 处理结果或保留理由 |
| --- | --- | --- |
| B1 | 已删除 | 删除旧预读分支、`read_wiki()` 和对应旧输出解析；研究阶段统一走有预算的取证，其他阶段明确报错。 |
| B2 | 已删除 | 删除额外技能注入接口及 `_skills` 字段，按阶段加载 `SKILL.md`。 |
| B3 | 已删除 | Python/CLI 只接受当前 `question/hypothesis/protocol/results`，删除 `metrics/reasoning` 别名。 |
| B4 | 已删除 | 自动续跑仅依据结构化 checkpoint；不再按旧文件组合或错误文本猜测轮次。 |
| B5 | 保留 | 这三个同义值来自真实模型响应，归一化仍用于当前取证及历史证据重验，并保留审计原值。 |
| B6 | 已删除 | 删除下表 12 份不参与当前加载和打包的旧技能稿。 |
| B7 | 已删除 | 删除 3 份仅供旧稿引用的参考文档、打包项及扫描器中过期的 injection-hygiene 文档指向。 |
| B8 | 保留；纠正初审判断 | 已找到独立前端实际读取，用于显示原始用户消息。 |
| B9 | 保留 | 更新时迁移仍在执行；没有确认所有共享库和历史页面都已迁移。 |
| B10 | 保留 | 仍承担历史关系读取与“未经验证假设”标记；默认库无此类边不代表其他共享库已迁移。 |

### B1：已删除旧 Wiki 预读分支

- 删除 `WorkflowRunner._legacy_wiki_context()` 以及仅为它引入的查询/读取导入。
- `_wiki_context()` 仅接受 `literature/ideas/novelty/research_refine`，统一调用 `collect_wiki_evidence()`；其他阶段抛出明确的 `ValueError`。
- 删除失去业务消费者的 `read_wiki()` 及专属 `location` 导入；删除 `observed_passages()` 中仅处理旧全页读取输出的分支，保留当前卡片/摘录处理。
- 将相关测试迁至统一查询，保留共享库范围、来源质量、实时版本、栏目位置与引用核验覆盖；旧工具不可注册/调用的负面断言保留。
- 影响：旧 Python 直接读取接口及非研究阶段预读不再可用。当前统一查询仍使用 `load_page()`、`paper_card()` 和关系扩展。

### B2：已删除未使用的额外技能扩展接口

- 删除 `PhaseAgent.add_skill()`、`_skills` 字段及覆盖默认阶段技能的分支。
- `_skill_prompt()` 直接按 `SKILL_BY_PHASE` 加载单个 `SKILL.md`，保留无文件时返回空字符串的行为。
- 保留工作流实际使用的 `add_tool()`。
- 影响：外部 Python 调用方不能再通过 `add_skill()` 注入额外技能。

### B3：已删除 Experiment 的旧字段别名

- 删除 `add_experiment()` 的 `metrics/reasoning` Python 参数、对应 CLI 参数和新字段为空时的回退表达式。
- 当前参数为 `question/hypothesis/protocol/results`，仍经内容隔离后写入四节正文。
- Python 与 CLI 测试均覆盖当前参数及四节正文。
- 影响：旧命令、外部 Python 调用需要将 `metrics` 改为 `results`、`reasoning` 改为 `protocol`。**实验 manifest 的 expected_outputs.metrics 指标校验保持不变。**

### B4：已删除旧审稿失败记录的推断恢复

- 删除由 `main_round0_original.pdf` + `PAPER_IMPROVEMENT_LOG.md` 推断续跑，以及由 `Paper review round 2 did not complete:` 错误文本猜测第二轮的分支。
- 自动恢复仅依据结构化 `improvement_checkpoint`，保留 `improvement_resume` 和 `--improvement-start-round`。
- 测试覆盖结构化第一/第二轮恢复，以及存在旧错误文本和快照但没有 checkpoint 时不会自动跳到第二轮；已有编译失败恢复测试继续保留。
- 影响：旧版无 checkpoint 的失败项目不再按旧文件或文本自动恢复，需明确选择重跑轮次。已同步 `docs/external-review.md`。

### B5：保留旧证据角色别名

- 位置：`paper_agent/agent/wiki_research.py:134`。
- 三个别名为 `boundary/condition` → `constraint`，`counterexample` → `counterevidence`；转换时写 `role_normalized_from`。
- 当前提示词已有规范枚举，但别名兼容有专门测试 `test_known_legacy_role_aliases_keep_audit_trace`，也用于重验历史证据包。
- 如未来明确放弃这些真实同义输入，需要同时删除归一化与审计字段写入，并将测试改为验证非规范角色被拒绝；本轮保留。
- 影响：模型输出或历史证据包使用这些同义值时会被判为无效，可能增加取证失败或历史证据过期提示。

### B6：已删除 12 份不被当前技能加载器使用的旧稿

`_skill_prompt()`、`skill()` 都读取 `SKILL.md`；`skill_resource_read()` 只允许 references/ 或 templates/。未找到当前 SKILL.md、源码或文档对这些旧稿文件名的引用；setuptools 的技能正文规则也只打包 `SKILL.md`。

| 文件 | 行数 |
| --- | ---: |
| `paper_agent/skills/paper-figure/skikl1.md` | 291 |
| `paper_agent/skills/paper-compile/skikl1.md` | 264 |
| `paper_agent/skills/experiment-plan/skikl1.md` | 248 |
| `paper_agent/skills/experiment-bridge/skikl1.md` | 357 |
| `paper_agent/skills/paper-write/skikl1.md` | 628 |
| `paper_agent/skills/research-lit/skikl1.md` | 645 |
| `paper_agent/skills/paper-plan/skikl1.md` | 410 |
| `paper_agent/skills/idea-creator/skikl1.md` | 444 |
| `paper_agent/skills/run-experiment/skikl1.md` | 304 |
| `paper_agent/skills/research-refine/skikl1.md` | 759 |
| `paper_agent/skills/auto-paper-improvement-loop/skill1.md` | 695 |
| `paper_agent/skills/novelty-check/skikl1.md` | 131 |

已删除共 **5,176 行**旧稿。删除的代价是失去仓库内旧技能原稿参考；`run-experiment/SKILL.md` 仍被执行流程使用，必须保留。

### B7：已删除仅供旧技能稿引用的 shared-references

- 文件：`paper_agent/skills/shared-references/output-versioning.md`、`output-manifest.md`、`output-language.md`。
- 当前 SKILL.md 没有引用这些文件；旧技能稿引用它们，其中前两份还描述根目录旧产物回退等 ARIS 约定。
- 已随 B6 删除这 3 份参考文档及 `pyproject.toml:28` 中只为它们保留的 `skills/shared-references/*.md` 打包规则。
- 影响：不再保留或分发旧输出协议参考；这不会自动清理运行目录中的 MANIFEST 或旧产物。
- `tools/threat_scan.py:21` 还提到已经不存在的 `shared-references/injection-hygiene.md`，已删除两处过期文档指向，保留威胁扫描功能。

### B8：保留 message_prompt，已确认前端消费者

- 初审仅搜索本仓库，误将 `runs.options.message_prompt` 判断为没有读取方；本次扩展核查后确认它是当前前端契约。
- 实际读取：`/home/xinghaohuang/paper-agent-web/src/features/workbench/Conversation.tsx:174` 与 `:182`，显示消息时优先使用 `message_prompt`，回退到 `prompt`。
- 同一组件 `:147` 会过滤带 `run_id` 的 `conversation_turn` 事件，因此仅保留事件副本并不能维持当前用户原话显示。协调请求的 prompt 可能拼入任务上下文，不能直接等同原始消息。
- 本轮保留后端写入和 worker 丢弃旧键逻辑。如后续删除，需要同时修改前端消息来源并验证历史运行展示。

### B9：保留旧 Wiki 正文的更新时迁移

- 位置：`paper_agent/tools/research_wiki.py:856` 删除旧 Paper 小节；`:1301` 删除旧 Idea Evidence 内容；`:662` 更新分类时清除旧 `research_object` 字段。
- 这是仍会执行的迁移逻辑，不是死代码。它使重新分析的旧页面符合当前正文结构。
- 当前仓库默认 research-wiki 中抽查统计为 2 篇 Paper、2 篇 Idea，没有旧 Paper 标题；这不能代表环境变量指定的共享库或其他项目都已迁移。
- 建议暂留。如果明确放弃旧页面处理，先核对实际共享库并完成一次性迁移，再删除这些分支和旧结构迁移测试。
- 影响：提前删除会在重新分析时留下旧 Results/Experiments/Evidence 正文，可能造成新旧内容并存。

### B10：保留已禁止新增、仍支持读取的 may_apply_to 关系

- 写入端：`tools/research_wiki.py:145` 明确拒绝 `may_apply_to`；当前关系生成器也不接受这一类型。
- 读取端：`tools/wiki_hybrid.py:36` 仍将其列为允许关系，`:450` 输出专门的 `unverified_transfer_hypothesis` 状态；`tests/test_wiki_hybrid.py:80` 起保留历史假设关系的方向和来源测试。
- 如果不再浏览这种历史关系，可删除读端枚举、文档字符串和专门状态分支，并明确跳过旧边；保留写端拒绝逻辑，避免旧类型重新写入。
- 本次读取当前进程默认库 `research-wiki/graph/edges.jsonl`，仅有 2 条 inspired_by、6 条 tested_by，没有 may_apply_to；未确认所有其他部署/共享库，故保留历史读取。
- 若后续删除，旧 graph 中这些边将不再参与关系扩展；应先统计实际共享库是否存在此类边。不要不加说明地把旧假设边变成普通已记录关系。

## C：发现了不一致，需先作功能决定，不建议直接删

### C1：Idea.stage / outcome 是“接收但不落盘”，不是已确认无用

- 写入链：`research_wiki.py:1164` 接收参数 → `:1190` 校验 → `:1123` renderer 接收但不使用 → `:1282` 仍记入日志。CLI `:1609`、WikiAgent `wiki.py:332` 仍传这些字段。
- 读取链：`get_stats()` 通过 outcome 统计成功/失败。审查时另有按 outcome 筛失败方向的 query pack，后续已按用户要求删除生成器及入口。
- 临时目录复现：传 `stage="archived", outcome="negative"` 创建 Idea，页面没有这两个字段，stats 显示 `1 (0 failed, 0 succeeded)`。
- 如果还需要“避免重复失败方向/统计结果”，应恢复 outcome 的持久化；stage 是否保留可单独决定。如果已放弃这组功能，则一起删除参数、枚举、CLI、调用方、日志字段和相关读端。
- 仅删除 renderer 的未用参数，或仅删除写端，都不能消除目前的功能不一致。

### C2：远程文献工具仍存在，但所有阶段均不可用

- `literature.py:146` 的 `search_papers()`、`:193` 的 `download_openalex_paper()`、`:257` 的 `download_arxiv_paper()` 都有 @tool 注册。
- 运行时注册表核对：这 3 项都存在；阶段白名单核对：**0 个阶段允许它们**。`tests/test_tools.py:153` 专门保证这一点。
- 与此同时，当前 `skills/research-lit/SKILL.md:14`、`:84`、`:85` 仍要求调用它们，`skills/TOOL_BOUNDARIES.md:11` 仍将它们列为 literature 工具。现有单元测试通过也不会暴露这种提示词与可见工具不一致。
- 若明确只使用本地来源，可删除这 3 项及仅由它们使用的远程请求/下载辅助函数，并同步清理当前技能和边界文档。保留 discover_local_papers/read_local_paper/read_paper_pages。
- 若需要在线补文献，应修复工具暴露和职责约定，不能把这部分直接当冗余删除。与 research_wiki 的 arXiv ingest 逻辑分开判断。

### C3：uv.lock 未跟随依赖更新

`pyproject.toml` 已要求 `prompt-toolkit>=3.0.48,<4`，当前 uv.lock 的本项目 dependencies/requires-dist 中没有它。需要同步锁文件；这属于依赖维护问题，不建议通过删掉依赖或整个锁文件处理。

## 已复核、建议保留

| 项目 | 保留理由 |
| --- | --- |
| `proposal_review.guard_legacy_approval()` | app、worker、workflow 都实际调用，用于防止已有审阅草稿被旧批准入口绕过；名称有 legacy 不代表保护已经过时。 |
| `ModuleData.sourceContent/disabled/needsReview` 等画布字段 | 本仓库没有逐字段业务读取，但会参与 Pydantic 校验、model_dump、审阅 payload、草稿哈希和持久化；独立前端契约未审查，不能按单次出现删除。 |
| `ModelResponse.raw` | `wiki_research.py:418` 通过 getattr 读取以获取 usage。 |
| `ExperimentExecutionError.receipt` | 定义文件中只写入，但 `experiment_queue.py:175`、`:251` 读取以区分执行完成与产物校验失败。 |
| `ToolSpec.read_only` | 协调器用于执行批次约束及失败处理。 |
| `RoleTerminal.last_output/help_text/folder/detail/busy` | 存在跨父子类读写，不是未使用字段。 |
| `load_page()`、`paper_card()`、`expand_wiki_relations()` | 当前统一 Wiki 查询仍调用的内部实现，不应与孤立的 browse_wiki 或 B1 的旧读取链一起删除。 |
| `@tool`、FastAPI 路由、model_validator、按键回调、CLI main | 装饰器或入口调用，不能以函数名只有一处定义判死。 |
| `TYPE_ALIASES`、老格式 arXiv ID、网络瞬态重试、BM25 回退、生图失败回退 | 分别承担真实输入归一化、合法标识支持与当前容错，不属于已证明多余的旧版本包袱。 |
| `dispatch.py:156` 的 `last_proposal` | 仅写无读，但明确是提交建议的审计快照。可以另行选择停止记录，不能仅凭没有执行读端判断其无价值；不是本轮优先删除项。 |

## 验证记录与后续审批

运行的现有测试文件：`test_wiki_quality.py`、`test_wiki_tools.py`、`test_wiki_research.py`、`test_wiki_experiment_schema.py`、`test_wiki_agent.py`、`test_workflow_cli.py`、`test_role_terminal.py`、`test_main_terminal.py`、`test_proposal_review.py`、`test_tools.py`、`test_literature_inputs.py`。命令使用项目实际运行环境 `/data/apps/micromamba/envs/xinghao/bin/python -m pytest -q -p no:cacheprovider`；170 项全部通过。未运行全部 750 项，未做前端联调或真实模型测试。

额外验证仅在临时目录创建合成 Wiki，复现 C1；导入本地注册表验证 C2，没有对真实 Wiki 写入。

用户已批准并完成 **A1–A8**。移除了 4 个孤立函数/方法、未读的任务字段、6 个未使用导入名及 2 个无用参数，并合并两份节点 ID 规范化实现。A 轮保留了后台任务创建、实际路径校验及当时尚未获批的 B 项；旧 Wiki 预读链已在后续 B 轮删除。

测试同步改为通过统一 `search_wiki()` 检查实时卡片、截断和范围限制，通过真实发布结果检查正文；保留证据、审核、纠错及旧工具不可见的断言。A8 另用 AST 比较确认提取后的辅助函数与两份原实现的函数体完全相同。

实施后验证命令：

```bash
/data/apps/micromamba/envs/xinghao/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_wiki_quality.py tests/test_wiki_tools.py tests/test_wiki_research.py \
  tests/test_wiki_experiment_schema.py tests/test_wiki_agent.py tests/test_workflow_cli.py \
  tests/test_role_terminal.py tests/test_main_terminal.py tests/test_proposal_review.py \
  tests/test_tools.py tests/test_literature_inputs.py tests/test_wiki_citations.py \
  tests/test_wiki_hybrid.py tests/test_wiki_relations.py tests/test_paper_agent.py \
  tests/test_data_access.py tests/test_paper_preprocessing.py tests/test_figure_workflow.py
```

A 轮结果：**283 passed in 5.46s**。该轮未运行全部测试，未做前端联调或真实模型测试。残留引用复核确认被删除符号没有生产调用；测试中保留 `browse_wiki` 不可注册/调用的负面断言。

B 轮已完成 B1/B2/B3/B4/B6/B7，保留 B5/B8/B9/B10。C1/C2 仍需先选择功能去留。

B 轮修改前全量基线：**768 passed, 25 warnings in 94.62s**。两项额外参数化用例分别覆盖当前 CLI 字段和无 checkpoint 的旧失败记录。

B 轮最终验证：

```bash
/data/apps/micromamba/envs/xinghao/bin/python -m pytest -q -p no:cacheprovider
```

结果：**772 passed, 25 warnings in 94.76s**。25 条 NVML 初始化警告与修改前基线一致。全量测试数量以当时工作区为准，工作区同时包含其他未提交改动。

另已通过 `git diff --check`、修改 Python 文件的 AST 语法检查、保留 package-data 模式的文件匹配检查，以及旧接口/参数/文档引用残留检查。AST 比较确认保留的 B5/B9/B10 核心函数体与本轮修改前一致。实际删除 15 份旧稿/参考文档，共 5,382 行。未修改真实研究数据或独立前端。
