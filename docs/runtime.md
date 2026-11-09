# 默认使用 xinghao

## 角色会话与阶段任务

研究执行采用四种角色：`main`、`idea_generator`、`experiment`、`paper_writer`。
同一项目、同一角色的阶段任务延续同一份模型对话；不同角色、不同项目不共享对话。
角色终端里的普通讨论使用另一份会话，不进入这些正式阶段任务的记忆。
默认分配如下：

| 角色 | 阶段 |
| --- | --- |
| main | 持续对话、串行派单、读取证据、安排后续步骤及请求用户决策 |
| idea_generator | literature、data_search、ideas、novelty、research_refine |
| experiment | experiment_plan、experiment_bridge |
| paper_writer | paper_plan、paper_figure、paper_write、paper_compile、improvement |

新研究、指定阶段、恢复、审阅回答及批准后的推进，统一由 `CoordinatorAgent` 分配任务。
`WorkflowRunner` 在主控委派中一次只执行一个节点，保留输入依赖、用户检查点、产物校验和实验执行回执。
工作流公开节点 `refine` 对应执行阶段 `research_refine`。独立审阅仍由独立审阅进程执行，
不并入作者的角色会话。所有现有 Skill 内容保持原样。

执行器统一接受显式阶段任务；主控或其他调用方可指定角色，覆盖默认分配：

```python
from paper_agent.agent import PhaseAgent, PhaseTask

agent = PhaseAgent(project)
result = agent.execute(PhaseTask(
    phase="novelty", role="idea_generator",
    instruction="读取当前方案，只检查其中创新性主张，并交付检查报告。",
))
print(result.role, result.session_id, result.task_id, result.final)
```

`go(instruction, phase=..., role=...)` 也使用相同的角色会话任务机制。
阶段工具与当前 Skill 随任务切换，不因角色覆盖而扩大；历史消息仅供参考，
当前任务和当前文件优先。任务结束后停止，由工作流或调用方决定后续安排。
直接调用使用阶段基础完成检查；正式工作流还会运行更完整的节点检查。
`PhaseTask.id` 可由调用方指定；已存在的 ID 拒绝重复提交，避免把重试误作新任务。

会话保存于项目 `.paper-agent/role-sessions/<role>.json`，包含稳定会话 ID、消息、任务状态
与任务结果。模型请求前、工具执行前、每次工具结果后及任务结束时持久化；同一角色有排他锁。
进程重启后重新加载磁盘记录；未记录结果的工具调用标记为不确定，要求核对文件、进程和回执，
不自动重放，也不把“没有回执”解释为“没有执行”。重新运行阶段会创建新的任务，
继承角色记忆后检查现有产物；不承诺从中断的模型轮次精确续跑。

角色会话和任务中的工作上下文共用摘要逻辑。任务中生成的摘要连同已覆盖消息的边界、
原文 SHA-256 和保留的任务指令位置，写入角色会话的 `context_memory`；摘要生成后、
下一次执行模型请求前即持久化。任务内的完整原文仍保存在 `messages`，验收仍读取原始任务消息。

下一次任务（包括中断后重新运行）先校验摘要对应的原文范围，直接采用摘要、保留的任务指令
和未覆盖的原始尾部，完整历史归档至 `role-sessions/archives/<role>/`。
只有尚无摘要或复用后的内容仍超过 `context_max_chars=120000` 时才调用模型；
已有摘要覆盖的原始正文不会再次送入摘要模型，新增尾部过长时以旧摘要和尾部生成更新摘要。
覆盖范围校验失败则回到原始历史；摘要失败不改写原历史及已有摘要。
这是字符阈值，并非精确 token 限额。
摘要可能有遗漏，原始项目产物和用户决策仍需重新核对。

工作流校验和节点工具统计只读取 `AgentState.task_messages`，防止历史工具回执被当作
本次任务证据。事件与节点结果包含 `role`、`session_id`、`task_id`，可沿用网页事件入口查看。
网页局部修改在候选工作区继承对应角色记忆；只有产物通过检查并发布后才更新项目会话，
被拒绝的修改不写入正式角色记忆。角色锁不等于跨角色文件锁，尚未开放多个作者并行写项目。

## 主控 Agent

网页新研究的默认入口以及研究对话中的执行请求进入后台 `CoordinatorAgent`。
主控与对话入口共享项目的 `main` 角色会话；对话入口只保存执行建议，后台队列通过
校验后才执行，建议不表示已提交或已完成。任务运行时主控会话有锁，消息入口返回
当前阶段提示，不并发修改主控记忆或提交第二项任务。

主控工具包括 `get_project_context`、`read_project_file`、`delegate_stage`、
`revise_artifact`、`revise_checkpoint`、`request_user_decision`。一次响应最多一个执行工具；
`delegate_stage` 指定 node、role、instruction，通过原工作流只运行该节点并返回结果。
阶段结果同时返回带 `sha256`、标题和短预览的 `artifact_handoff`；主控先用交接摘要验收，
只有需要核对具体证据时才读取原文件。`read_project_file` 返回 `next_offset` 和文件哈希，
同一次主控调用中对相同哈希、相同范围的重复读取会返回缓存提示，避免把长报告重复放入上下文。
主控回执只返回有限的节点标识及状态字段，不再附带 `node_results.artifact` 中的报告全文；
`workflow-state.json` 的 `artifacts` 仅保存各节点产物的路径、字节数与 SHA-256，
`node_results` 保存执行元数据，不再保存报告正文。批准用户编辑后的文件时刷新产物引用；
下游通过真实文件交接。文献阶段把采集正文保存到
`literature/SOURCE_CONTEXT.json`，初始任务只注入来源目录、哈希和短预览。

模型工作上下文与完整执行记录分开：旧的大段写入参数、完全相同的成功读取结果可以在
请求副本中缩减；最近交互和失败结果保留。每次模型请求前检查对话长度，超过预算时
摘要较早且已经闭合的交互，保留当前指令和近期工具交互。固定 Skill/system 提示及近期
交互不强行截断，因此字符预算是压缩触发阈值，不是严格的 token 上限。完整会话、
当前任务验收记录不受工作上下文压缩影响。事件 `context_prepared` 记录原始及请求字符数，
`working_context_compacted` 标记节点内摘要；Skill 内容保持原样。
原工作流检查输入、产物和实验回执；主控可覆盖默认执行角色，但不能扩大当前阶段工具。
`refine` 即使在自动模式也强制生成专家检查点，须经原审阅入口批准。
用户检查点未批准时，主控不能派发下游；用户通过审阅入口提交修订意见后，
仅允许主控委派该检查点对应节点。方案修订后仍须再次批准。

主控默认每次调用最多 24 轮模型回复、6 次任务执行，各执行者仍使用单阶段轮数预算。
收到失败、中断、等待结果或等待用户状态后，主控停止后续执行并总结；
单独阶段/局部修订请求在后端绑定任务参数，不能替换任务或扩展至其他阶段。
主控和执行角色均可用 `request_user_decision(question, options)` 提问。Main 终端实时显示
来源角色、任务 ID、问题 ID 和选项，角色保留原任务与工具回执等待。用户在 Main 输入
`/answer 回答内容`（只有一个问题时）或 `/answer 问题ID 回答内容`，答案只送回该问题的原任务，
同一任务从等待点继续，不重跑已完成的工具。`/status` 或普通查询不会被记作答案。
问题和答案保存在 `.paper-agent/user-questions/`，重复、失效、项目版本变化后的答案会被拒绝。
等待人回答的时间不计入 Herdr 阶段执行超时。
Ctrl+C 取消任务会同时使问题失效；进程异常退出后的任务不自动重放，需要核对已有回执后重新派单。
非交互调用主控默认只记录问题并结束本轮，可通过显式 `/answer ...` 开始后续轮次；
角色提问始终等待。外部终端可运行
`python -m paper_agent.agent.user_questions --project PROJECT --answer '回答内容'` 回答或省略 `--answer` 查看待答问题。
科学方案检查点仍须经正式审批逻辑批准，问题答案不替代方案批准。
Herdr 主控终端已接通此逻辑：在待审批时输入“批准”“批准推荐方案，继续实验规划”或“可以的 开始”，
终端先核验提交时的检查点和报告版本，调用工作流审批并保存确认哈希，再让主控继续。
方案批准仅授权进入实验规划；否定、疑问和附带修改要求的输入仍作为普通对话，不会自动批准。
待审节点的修改要求由主控使用 `revise_checkpoint` 发回原阶段负责角色，生成新稿与新的审阅摘要，
然后重新等待批准。`revise_artifact` 保持精确文件范围；修改已批准方案或当前检查点文件会重开审批。
终端批准时同时核验工作流和所有检查点文件版本，旧版本的批准不能应用到新稿。

产物内容版本保存于 `.paper-agent/artifact-versions.json`，主控上下文和 `/status` 显示 `stale_nodes`。
上游产物内容变化时，已有的后续阶段产物保留在磁盘，但标记为需要重新核查或生成，
不能直接批准或绕过过期输入继续执行。依赖按工作流顺序保守处理；验证完成的重新生成只解除本节点失效，
后续节点依次处理。文件内容未改变不增加版本、不额外使下游失效。手工编辑待审稿后明确批准的既有用法保留。
主控摘要和子任务进度沿用 Web 事件流，不需要新增前端页面。

`paper-workflow` 和 `paper-agent --phase` 同样先进入主控，再委派明确节点。
审批先校验并持久化用户确认的文件，再交回主控；批准方案只授权实验规划，不自动运行实验。
用户的流程停止边界 `workflow_stop_after` 与执行器单节点的 `stop_after` 分开保存，避免批准后提前结束。
终端也可直接调用主控：

```bash
/data/apps/micromamba/envs/xinghao/bin/python -c 'from paper_agent.agent.coordinator import main; main()' \
  --project /path/to/project '调研这个研究方向，形成方案，交给我审阅'
```

安装当前包后也可用 `paper-coordinator`。主控只在委派真实实验阶段时运行 CUDA 环境检查，
纯文献与写作不要求先运行 GPU 运算。CPU-only 实验队列不调用 NVIDIA 管理工具；
CUDA/自动设备实验仍保留真实探测，不因探测失败回退 CPU。

统一入口固定使用 `/data/apps/micromamba/envs/xinghao/bin/python`，并将该环境的 `bin` 放到 PATH 最前面。无需先激活环境，当前终端即使激活了其他环境也不会改变工作流解释器。

```bash
cd /home/xinghaohuang/paper-agent
./run-workflow --check-env
./run-workflow --project ./demo-project16 --max-tool-rounds 100 --timeout 600 '研究任务'
```

入口在工作流启动前检查依赖、CUDA 可见性和一次真实 GPU 运算，失败即退出。它不会回退到 `.venv` 或 CPU。检查遵循现有 `CUDA_VISIBLE_DEVICES`，不会擅自改变 GPU 可见范围。

工作流执行器使用 `sys.executable` 执行 manifest 中以 `python` / `python3` 开头的任务，因此从 xinghao 启动即可让这些实验沿用 xinghao。shell 工具通过 PATH 使用同一环境。显式写死其他解释器的实验命令仍需人工纠正。

CUDA 可用并不保证每个算法使用 GPU：数据处理和 CPU 基线可以使用 CPU；PyTorch 训练必须将模型和张量放到 CUDA。不得以环境缺失为由替换既定模型。

修改入口不会迁移正在运行的进程，也不会改写已生成的训练代码。

## 模型连接失败重试

模型请求遇到连接中断、读超时、`Upstream HTTP/2 stream failed`、流缺少结束标记，或 HTTP 408/409/429/5xx 时，从首次失败开始提供 300 秒重试窗口。退避等待为 2、4、8、15 秒，之后保持 15 秒；等待和重试请求耗时都计入窗口。重试沿用当前对话及已完成工具结果，不重新进入节点，不重复执行已完成的工具调用；中断流的局部内容和工具调用丢弃。

重试请求的网络超时限制为窗口剩余时间，读取流时也检查截止时间。网络超时按底层传输操作生效，退出时间可能稍晚于 300 秒；首次请求的等待仍由 `--timeout` 决定，不计入后续重试窗口。认证失败、权限不足、请求参数错误等非临时错误直接失败。窗口耗尽后节点仍保存为 failed，可按已有恢复方式继续。原有输出截断和无效工具参数的修正重试保持独立。

此策略用于工作流的 ModelClient 请求，关闭其内建 SDK 自动重试以避免嵌套等待；外部工具各自发起的网络请求不受此策略控制。已运行的进程需要重启才能加载更新。

## 分节点运行与补充指令

项目总目标只保存为 `project_goal`，由主控单独传给执行器。主控的 `assignment.instruction` 只说明本次阶段要求；执行器不再通过比较 prompt 与历史文本来猜测其含义。两个字段在阶段提示中分别标注，总目标不会被首次派单或修订意见覆盖。

阶段指令只保存在 `node_results.<node>.instruction`，移除顶层 `node_instructions` 及 history 中的重复正文。后续阶段不会继承上一阶段指令。主控的绑定任务只注入一次，后台不再在用户 prompt 后重复拼接任务 JSON。

在已有项目中使用 `--start-node` 时，命令中的文字作为本次主控请求，可用于指定该阶段的要求；项目总目标保持不变。

例如实验已完成后，只运行画图节点：

```bash
./run-workflow --project ./demo-project21 --start-node paper_figure --stop-after paper_figure '图1采用横向三面板，突出弛豫残差分支；结果图使用色盲友好配色'
```

这会使用已有项目目标、计划和结果，从 paper_figure 开始并在该节点完成后停止。没有 workflow-state.json 时，新输入的 prompt 作为原始目标。

## 当前配置和任务接口

### 专家模式（每个节点均可审阅）

默认 `--mode auto` 自动运行。`--mode expert` 在需要审阅的节点完成后保存 `awaiting_user` 状态，在交互终端显示摘要和 2–3 个研究取舍问题，并提供统一输入入口：

```bash
./run-workflow --project ./demo-project22 --mode expert '你的研究目标'
```

- 输入回答：先保存回答，再让当前节点修订产物，整合明确选择、消除冲突。修订成功后自动确认最新报告并进入下一节点，不再次询问；修订失败不放行下游。
- 直接编辑终端列出的当前节点文件：保存后在终端回车，系统重新读取最新版并进入下一节点，不调用模型重写。
- 不需要修改：直接回车，接受当前报告并继续。
- Ctrl+C 或 EOF：保留检查点退出。没有交互终端时也保存检查点退出。

退出后使用相同入口返回审阅，不用重复研究目标，也不需要其他恢复参数：

```bash
./run-workflow --project ./demo-project22
```

已移除 CLI 的 --continue/--revise 及终端的斜杠命令。所有输入文字均作为回答；输入为空时使用当前磁盘文件。等待期间不设自动超时，手动编辑文件后需要回车提交。摘要和问题保存在各节点主产物目录的 `<节点>_EXPERT_CHECKPOINT.json`（literature 使用 `literature/EXPERT_CHECKPOINT.json`），检查点、原始研究目标、确认哈希和停止位置保存在 `workflow-state.json`。确认时更新内存中的报告副本供下游使用；所有节点均设置检查点，最后的 improvement 确认后完成工作流。主流程 skill 未修改。

专家检查点覆盖 literature、data_search、ideas、refine、experiment_plan、experiment_bridge、paper_plan、paper_figure、paper_write、paper_compile、improvement。refine 交付一句话摘要和四项可回查正文的摘录。提交意见只修订并返回复审，明确批准当前版本后才可进入实验规划。指定 --stop-after refine 时，确认后停止。其他节点即使设置 --stop-after，也先审阅目标节点，确认后停止。实验计划问题围绕具体区块的增删、优先级和实验设置修改；paper_plan 在计划正文及审阅问题中展示全部计划图的完整图注草稿，供用户按图编号逐张确认或修改。每次确认重新读取磁盘文件、校验主要产物存在并非空，记录多个主要文件哈希；ideas 等以内存内容传递的产物同步刷新。experiment_plan 确认时刷新 tracker；experiment_bridge 核对 manifest、执行回执、依赖和产物，未完成任务不放行；paper_compile / improvement 确认时重新编译当前源码以同步 PDF。手动编辑实验结果不等于执行了实验，训练或任务变更须在回答中要求当前节点执行。

计算和执行策略只读取项目根目录的 `project-config.json`，分别放在 `compute` 和 `execution` 对象中；文件缺省时使用当前默认值。`experiments/resources.json` 不再读取，也不再作为用户计算策略文件保护或纳入策略证据快照。

每个 manifest job 必须声明 `depends_on`：独立任务填写 `[]`，有依赖的任务填写上游 job ID。缺少字段会被拒绝，不再根据 manifest 顺序推断串行依赖。

实验完成状态统一保存在 `experiment_completed_receipts`，实际执行回执仍写入 `experiment-runs/*/status.json`，供当前任务恢复及预算统计使用。不再读取或写入 `node_results.experiment_run.jobs`。队列进度使用 `experiment_bridge` 阶段名。

任务必须进程退出成功且所有 `expected_outputs` 校验通过，才记为完成并放行下游任务。产物文件必须存在且字节数非零；JSON 必须是对象或数组，允许 `{}` / `[]`，CSV 允许只有表头。对应条目可声明 `"nonempty": true` 要求集合或表格非空；`metrics` 声明的字段仍必须存在且具有有效有限数值。校验失败会保存失败回执并返回 bridge 内部修复。

tracker 的“完成/总数”表示产物及依赖均通过校验，“进程成功退出”单独保留执行事实。刷新发现产物缺失或不合格时显示“产物校验失败”，已经执行但依赖不再有效的任务显示“依赖产物无效”，问题明细列出任务和原因；不会把已执行任务直接还原成“待运行”。

CLI 使用 `--max-tool-rounds`，不再接受 `--max-iterations`。PhaseAgent 和工具权限接口仅接受当前 phase 名称 `research_refine`、`experiment_bridge`，不再接受 `refine`、`experiment` 别名；公开 workflow 节点 `refine` 保留，它在内部调用 `research_refine` phase。

Skill 资源读取工具要求显式 `references/文件名` 或 `templates/文件名`，不再补全裸文件名。Wiki 使用已安装的 `paper-wiki` 入口或 `python -m paper_agent.tools.research_wiki`，必需的扫描模块直接从包内导入。Wiki 文件要求 UTF-8，不提供旧编码处理。Zotero 库 ID 使用 `--zotero-library-id` 或 `ZOTERO_LIBRARY_ID`，不再回退到 `ZOTERO_USER_ID`。

旧 checkpoint 节点重定向、退役字段过滤及 Wiki schema 迁移脚本均已移除，不对已有项目自动迁移。主流程 SKILL.md 按用户要求保持原文；其中省略 `depends_on` 时串行执行的旧描述不再适用，实际以运行器校验及其注入的接口说明为准。

## Figure 1 交付声明

Figure 1 启用时，`figures/figure_manifest.json` 使用 `{"figure_1":{"asset":"figures/figure_1.png","preview":"figures/figure_1.png"}}` 声明最终路径，文件名可自选。生成 PNG 的 asset 和 preview 指向同一实际输出，必须有本次生成成功回执、匹配哈希、最终 LaTeX 引用和视觉检查。代码绘图回退时 asset 指向 PDF、preview 指向 PNG，回退清单保持相同路径。未交付草稿不参与最终生成回执校验，同路径多次生成使用最新成功回执。

## 生成 Python 文件的格式化

公共 `write` / `edit` 工具在保存 `.py` 文件前使用 Black 默认格式化，统一缩进、空格、换行并展开分号拼接的语句。工具返回值会提示格式化；后续精确文本替换应先读取当前文件，不能继续假定原始压缩文本仍存在。

语法不完整或 Black 无法解析时保留本次内容并提示跳过，便于继续编辑修复；这不代表代码通过验证。非 Python 文件保持原文。此逻辑不扫描历史文件，也不处理通过 `bash` 或外部脚本直接写入的文件；尚未接入资源库中的代码设计规范。

默认每次阶段 Agent 调用最多 100 轮模型回复，网页和命令行一致；每次阶段调用重新计数。网页审阅继续使用本次请求的轮数（未指定时为 100），不继承历史运行的旧轮数。显式设置仍可覆盖默认值。已运行中的进程保持启动时配置。

## Herdr 常驻角色 Agent

终端交互的默认入口是主控 Main。在 Herdr 管理的终端内运行：

```bash
paper-main --project /path/to/project --session SESSION_NAME
```

Main 使用真实 CoordinatorAgent，用户直接与主控的持久会话对话。启动只创建主控
界面，不调用 team、不预启动三个角色；主控调用 delegate_stage 时才按需创建对应
角色分屏，后续复用该角色。主控对话区显示派单、阶段回执和最终答复。
角色分屏用于观察执行，需要时可单独讨论，日常任务从 Main 输入。
Main 默认占满终端，宽度至少 120 列时角色派单提交后展开执行分屏，较窄时保持 Main 放大并显示角色进展。
每个阶段回执到达后立即收起分屏，回到 Main 核对结果；下一次派单时再展开，不必等主控整轮结束。角色进程与
独立会话保留供后续任务复用。点击只读消息区后直接打字或粘贴会自动返回输入框，
也可按 Esc 返回输入，避免误以为终端无法输入。
主控和执行角色的公开回复、简短进展说明逐段流式显示，工具动作、派单及回执实时显示。
输入框上方保留动态状态区，显示正在思考、等待模型响应、正在回复或当前工具，以及当前步骤耗时、
本轮耗时、模型轮次和最近事件时间。心跳只更新状态，不重复刷屏；不展示模型内部推理链。
终端每 100 毫秒独立读取事件文件，子进程没有标准输出时也能及时展示模型与工具进度。
底层只在流完整结束且工具参数校验通过后执行工具。网络重试会替换未完成的回复，
不会把半截工具调用执行出去。`/logs` 保留底层执行日志。
新项目可用 `--goal "研究目标"` 设置持久目标（保存于主控终端的 project.json）；
已有 workflow-state.json 中的项目目标仍优先。测试工作区和正式研究应使用不同项目
目录，避免测试目标、回执与角色历史进入科研上下文。
`paper-coordinator "任务" --project ...` 保留为一次性命令入口。
Ctrl+C 优先清空草稿；再次按下请求取消当前主控轮次，等待该轮已有执行者回执。
不跨会话自动迁移项目；已有项目需使用其绑定的 Herdr 会话。

主控终端在 `paper-agent-main` 会话通过真实模型验证：首次仅讨论时没有创建角色，
随后主控按用户要求仅派发 literature，自动创建 idea_generator，核对实际文件后
结束。主控、终端及 Herdr 等 90 项定向测试通过；科研 Skill 内容未修改。

主控按节点派发到三个项目专属角色：idea_generator、experiment、paper_writer。
配置 Herdr 的项目所有阶段均通过对应常驻角色执行；未配置的项目继续本地执行。
阶段顺序、角色会话、Skill、审批和产物验收仍由 paper-agent 负责，未修改科研 Skill。

在 Herdr 管理的终端中运行：

```bash
paper-herdr connect --project /path/to/project --session SESSION_NAME
paper-herdr team --project /path/to/project
```

team 创建或核验三个独立角色终端；主控也可在首次派发时按需创建对应角色。
每个角色在独立 Python 进程中常驻，后续任务复用同一分屏和角色会话。
程序通过 pane report-agent 上报角色标签及 idle/working/blocked/unknown 状态，
通过 agent rename 登记项目唯一名称。Herdr agent list 可看到三个角色。
这是自研 Agent 的显式身份上报，不是假装成 Codex；Herdr 0.9.5 的 agent prompt
对当前自研角色仍返回 agent_not_ready，因此主控不使用该接口投递任务。

角色终端使用 prompt-toolkit 全屏交互界面：顶部显示角色、状态和执行耗时，中间为
可滚动的对话记录，底部固定输入框。布局随分屏大小收缩。Enter 发送，Alt+Enter
或 Ctrl+J 换行，PgUp/PgDn 浏览消息，Ctrl+F 搜索，Esc 返回输入。
`/status` 查看状态，`/logs` 查看最近执行日志，`/clear` 清屏，`/help` 查看帮助，
`/quit` 退出。Ctrl+C 优先清空草稿；草稿为空且阶段执行中时请求取消并等待回执。
执行期间可继续编辑草稿，派单不会覆盖输入。角色直接对话使用独立的
`.paper-agent/role-sessions/discussions/<role>.json`，只讨论不调用执行工具，也不写入正式执行记忆。
要把讨论中的建议用于后续产物，请在 Main 提出修改要求，由主控正式派单。
历史混合记录首次用于正式执行时会归档到 `archives/<role>/legacy-mixed/`，仅保留可识别的正式任务交互，
不复用可能混有闲聊的摘要；无法识别的旧讨论仍留在归档供核对。
主控执行中拒绝并发新任务，但接受 `/answer` 回答当前问题和 `/status` 查看进度。
角色进程退出后保留分屏；绑定失效时报错，不自动重启或重投未知任务。
更新代码后，已有 Main 和角色常驻进程需要在当前任务结束后重启，才能加载新的界面及讨论隔离逻辑。


主控把节点指令写入任务 request.json，通过角色 inbox.json 投递；不会向终端自动
键入任务，因此不会覆盖人的输入草稿。角色领取任务后调用现有单阶段执行器，
将事件写入 events.jsonl，状态及产物写回项目，最终写 result.json。
主控校验任务 ID、工作流状态 SHA-256、产物 SHA-256，再将 active 标记 consumed。
Herdr 的 idle 状态不替代科学验收和执行回执。

Web 在 Herdr 终端运行 `./run-herdr-web SESSION_NAME`。启动器核验 caller pane，
设置 PAPER_AGENT_HERDR_SESSION；新项目首次派发时自动绑定该会话。
已有项目的显式绑定优先。API 使用 PAPER_AGENT_API_PORT，前端沿用现有网页。
主控目前仍在 Web 后台运行，三个执行角色在 Herdr 常驻，不增加额外原生模型客户端。

角色绑定、ready、host.lock、inbox.json 位于项目 `.paper-agent/herdr/roles/<role>/`。
绑定使用进程启动 generation、实际 PID/命令行、项目 cwd 及 pane 身份核验复用对象。
任务文件位于 `.paper-agent/herdr/tasks/<task-id>/`，包含 request、started、events、result。
项目 transport.lock 与角色 host.lock 阻止并发投递及重复角色进程。
投递响应丢失、等待超时或硬退出不会自动重投，项目保留未核清任务：

```bash
paper-herdr status --project /path/to/project
paper-herdr wait --project /path/to/project TASK_ID --timeout 300
```

status/wait 只读取项目任务文件，不控制 Herdr，允许在外部终端核清回执。
中断等待写合作式取消标记，角色内执行器记录 interrupted；超时不等于取消。
硬崩溃且无结果时需人工核查原分屏与产物，不能推断任务从未执行。
旧的仅文献配置不继续兼容，需重新 connect；未核清任务必须先 wait。

2026-11-09 在独立 paper-agent-roles 会话验证三个角色出现在真实 agent list，
文献角色真实模型写文件并返回 completed；后续 Web 派单复用同一分屏和角色会话。
写作角色通过终端输入完成直接对话并保存角色历史。这些是连通测试，无科研结论。
84 项定向测试通过，覆盖主控、Web、回执、角色归属、复用身份、信箱连续派发和未知投递。此前全量 18 项失败
仍未在本轮修复；不据定向测试声称完整科研阶段端到端全部通过。

终端交互界面在 `paper-agent-ui` 会话中验证：三个角色正常识别，真实文献连通任务
返回 completed 且未发送的草稿保留，写作角色直接对话成功。80×24 和 180×54
终端分屏均可渲染。新增输入、换行、草稿、取消及控制字符过滤测试后，87 项定向
测试通过。测试项目为 terminal-ui-demo，不产生科研结论。
