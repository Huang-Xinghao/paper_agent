# 默认使用 xinghao

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

首次运行的 prompt 保存为项目原始研究目标 `task_prompt`。使用 `--start-node` 读取已有项目状态时，可以传入不同的 prompt，作为本次入口节点的补充指令；原始目标保持不变。节点同时收到研究目标和补充指令，后续自动路由的节点只接收原始目标。当前补充指令保存在 `node_instructions`，实际执行记录也保存在节点结果及 history 中。再次运行时不会自动沿用上次的补充指令。

例如实验已完成后，只运行画图节点：

```bash
./run-workflow --project ./demo-project21 --start-node paper_figure --stop-after paper_figure '图1采用横向三面板，突出弛豫残差分支；结果图使用色盲友好配色'
```

这会使用已有项目目标、计划和结果，从 paper_figure 开始并在该节点完成后停止。没有 workflow-state.json 时，新输入的 prompt 作为原始目标。

## 当前配置和任务接口

### 专家模式（每个节点均可审阅）

默认 `--mode auto` 自动运行。`--mode expert` 在需要审阅的节点完成后保存 `awaiting_user` 状态（refine 完成后直接进入 experiment_plan），在交互终端显示摘要和 2–3 个研究取舍问题，并提供统一输入入口：

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

专家检查点覆盖 literature、data_search、ideas、experiment_plan、experiment_bridge、paper_plan、paper_figure、paper_write、paper_compile、improvement。refine 不生成专家问题或暂停审阅；显式设置 --stop-after refine 时，完成后直接停止。其他节点即使设置 --stop-after，也先审阅目标节点，确认后停止。实验计划问题围绕具体区块的增删、优先级和实验设置修改；paper_plan 在计划正文及审阅问题中展示全部计划图的完整图注草稿，供用户按图编号逐张确认或修改。每次确认重新读取磁盘文件、校验主要产物存在并非空，记录多个主要文件哈希；ideas 等以内存内容传递的产物同步刷新。experiment_plan 确认时刷新 tracker；experiment_bridge 核对 manifest、执行回执、依赖和产物，未完成任务不放行；paper_compile / improvement 确认时重新编译当前源码以同步 PDF。手动编辑实验结果不等于执行了实验，训练或任务变更须在回答中要求当前节点执行。

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
