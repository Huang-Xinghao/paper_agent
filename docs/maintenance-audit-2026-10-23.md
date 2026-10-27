# 兼容、依赖与冗余检查（2026-10-23）

本文前七节为首次检查快照，后续变更见第八节起；当前接口见 `docs/runtime.md`。用户后续授权移除旧兼容，无需迁移或复用旧项目，早期的兼容保留建议已被此决定取代。

本次仅检查并新增此报告，没有修改运行代码、依赖、skill 或已有研究项目。范围为 `paper_agent/`、`pyproject.toml`、`uv.lock`、`run-workflow`、`scripts/`、`tests/` 和项目文档；未逐项审计电池资源库中的第三方研究代码，也未查询软件包上游的最新版本。

## 1. 可以优先处理的依赖

| 项目 | 证据 | 建议 |
| --- | --- | --- |
| `langgraph>=0.3` | `pyproject.toml:11` 声明，`run-workflow:20` 强制导入；业务 Python 没有 LangGraph import。`agent/workflow.py:108` 仍写“将来迁移到 LangGraph” | 移除声明和启动检查中的导入，更新项目描述，再重新生成锁文件 |
| `fonttools>=4` | 同上，只有启动脚本导入 `fontTools`，没有业务调用 | 从主运行依赖与启动检查移除；未来确有字体检查功能时再按功能引入 |
| 依赖下限过宽 | `openai>=1.0`、`langgraph>=0.3` 等与锁文件版本相差很大；没有证据表明这些最低版本经过验证 | 如保留开放下限，验证最低支持版本；否则按实际使用的 API 调整。不能仅凭下限旧就认定安装版本旧 |
| 固定环境没有遵循锁文件 | `run-workflow:5` 固定 `/data/apps/micromamba/envs/xinghao`，不校验锁文件。检查时已安装 LangGraph 1.2.14 / OpenAI 3.26.0 / Pillow 12.3.0，锁文件分别为 1.2.12 / 3.24.0 / 13.0.0 | 明确 micromamba 环境与 uv.lock 哪个负责生产复现，并提供一致性检查 |
| 系统及专用环境依赖分散 | 启动必需 torch/CUDA；队列调用 `nvidia-smi`；编译调用 `latexmk`；审稿调用外部 CLI；全文读取调用独立环境里的 MinerU，分图脚本使用 `pypdfium2` 和 Pillow | 区分基础 Python 包、GPU 运行环境、论文编译工具、审稿 CLI 和 MinerU 专用环境，按所需阶段做环境检查 |

按当前锁文件的依赖图，在保留其余直接依赖和 pytest 的前提下，LangGraph 独占的依赖节点共 **22 个，含自身及 21 个间接依赖**：`charset-normalizer`、`distro`、`jsonpatch`、`jsonpointer`、`langchain-core`、`langchain-protocol`、`langgraph-checkpoint`、`langgraph-prebuilt`、`langgraph-sdk`、`langsmith`、`orjson`、`ormsgpack`、`pyyaml`、`requests`、`requests-toolbelt`、`tenacity`、`urllib3`、`uuid-utils`、`websockets`、`xxhash`、`zstandard`。实际删除集合以重新解析锁文件为准。

`openai`、`pypdf`、`httpx`、`Pillow`、`black` 均有实际业务调用，不应作为无用依赖删除。Black 是公共 write/edit 工具的运行时功能，不能直接移到开发依赖。`pypdfium2` 在本次主环境未安装，但分图脚本是由 MinerU 的 Python 启动，不能据此判定当前功能必然失败；应检查那个专用环境。

## 2. 仍在工作的旧兼容

| 兼容点 | 位置 | 处置条件 |
| --- | --- | --- |
| 过滤旧 checkpoint 的 6 个字段 | `agent/workflow.py:71`：review、experiment_review_status、revision_count、idea_candidates、selected_idea、last_experiment_failure | 先一次性迁移要保留的旧 checkpoint，再删运行时过滤 |
| 旧节点重定向 | `agent/workflow.py:176`：experiment_check/run → experiment_bridge；novelty → refine；`start_from_project:293` 又保留 check/run 转换 | 将旧节点转换集中到 checkpoint 迁移边界，避免运行入口各自处理 |
| 旧计算资源文件 | `project_config.py:19` 回退到 `experiments/resources.json`；`tools/core.py:55` 和证据快照也保留该路径 | 把旧配置迁到 project-config.json 后统一入口 |
| 旧实验回执键 | `agent/experiment_queue.py:94`、`agent/workflow.py:813` 仍从 node_results.experiment_run.jobs 恢复 | 迁移到 experiment_completed_receipts 后删除旧读取；队列当前还主动写两份状态 |
| 隐式串行 job 依赖 | `agent/experiment_executor.py:32`：缺 depends_on 时默认依赖前一项 | 这是有意保留的行为，不是纯死代码。迁移 manifest 时显式写依赖，再考虑强制该字段 |
| CLI 参数旧名称 | `agent/loop.py:392`、`agent/workflow_cli.py:129` 支持 --max-iterations | 成本很低，可在统一 CLI 时移除；优先级低于依赖和 skill 资源问题 |
| phase 别名 | `agent/loop.py:configure` 和 `tools/profiles.py:tools_for_phase` 都转换 refine / experiment | 集中成一个标准化函数；refine 目前也是公开 workflow 节点名，不能直接删所有转换 |

兼容建议集中在一个有版本号、可独立运行的迁移入口，避免继续散落在工作流、队列和工具中。是否停止支持旧文件，应以用户仍需恢复的任务为依据。

## 3. Workflow 的冗余与遗留

1. **已退出路由的私有节点实现仍在。** `workflow.py:603` 的 `_run_novelty`、`:594` 的 Markdown renderer，以及 `:837` 的 `_run_experiment_run` 不在 `_nodes` 中；仓库内主要由测试直接调用。独立 novelty skill 是文档明确保留的能力，不能连 skill 一起删除；可把其校验和独立执行移到独立服务，主 workflow 删去不可达节点包装。
2. **无有效业务消费者的状态字段。** `workflow.py:63` 的 experiment_expansion_requested 只声明及重置，experiment_expansion_count 只声明。这是旧扩展流程留下的状态。迁移旧 checkpoint 后可删除。
3. **节点定义有多份。** `_nodes`、`_next_node`、`WORKFLOW_NODE_NAMES`、`SKILL_BY_PHASE` 分别维护节点/顺序/公开名称/skill。输入输出路径又分布在 `start_from_project`、`_validate_phase_outputs`、`AgentLoop._check_completion` 和节点正文。建议一个简短 NodeSpec 注册表描述当前稳定信息；各阶段专属科学校验保留在自己的函数中。
4. **输出校验有重复执行和副作用。** completion_check 已验证输出后，节点又读取/检查同一文件；实验计划在 `_validate_phase_outputs:264` 和 `_run_experiment_plan:681` 都生成 tracker。宜让输出验证保持只读，在成功边界统一提交 tracker。输入验证、进程结束后的产物验证仍有各自用途，不宜全删。
5. **恢复回执算法重复。** `_refresh_experiment_tracker:810` 与 `experiment_queue._schedule` 都加载完成回执、匹配 job、验证产物、级联剔除缺失依赖。两者一个生成 tracker、一个决定调度，但应共享“有效已完成任务集合”的计算。
6. **状态有同义副本。** 队列 checkpoint 同时写 experiment_completed_receipts 和 node_results.experiment_run.jobs；一般节点的 artifact 又出现在 artifacts 与 node_results，history 再存 metadata。可先统一回执真源，再让展示视图派生，保留必要的时间历史。
7. **阶段工具遗留。** `tools/profiles.py:31` 的 writing、`:41` 的 paper_figure_design/review 在当前生产路径未发现调用，相关断言主要在测试中。作为外部 API 保留与否需明确；若无调用方，可连旧测试删除。
8. **CLI 重复。** loop.main 与 workflow_cli.main 都解析 project/model/轮次/timeout/idea 参数，然后都进入 run_workflow。可共用参数注册或将单节点操作统一为 paper-workflow 的参数。
9. **少量无用 import。** workflow.uuid、loop.time、model_client.field、figure_grouping.Image 可清理。`__init__` 的导出、工具模块的注册导入，以及 loop 中显式公开的 ToolCall 不是普通无用 import，不能机械删除。

## 4. Skill 的冗余与可用性问题

### 4.1 旧稿占了主要维护噪声

skills 下有 **12 份 `skikl1.md/skill1.md`，合计 5,176 行，约 272 KiB**。`PhaseAgent._skill_prompt` 和 `skill()` 仅加载 SKILL.md；pyproject package-data 也只收集活动 SKILL.md 和指定资源，未发现运行代码引用这些旧稿。

这些旧稿含 ARIS 命令、旧 MCP、旧输出结构和旧执行策略。建议移出活动 skills 树并单独归档，或在有可靠版本历史后删除。它们造成仓库和搜索噪声，**不等于每轮都消耗模型上下文**。

### 4.2 三份共享协议基本未接入

`shared-references/output-language.md`、`output-versioning.md`、`output-manifest.md` 未发现活动 SKILL.md 或运行代码引用；目前统一注入的只有 project-evidence.md。

旧协议仍描述 CLAUDE.md、ARIS、旧 review-stage 和根目录路径回退。output-manifest.md 还链接不存在的 output-composition.md。这些文件现在主要是失效文档，不应误报为正在生效的路径兼容。建议删除未使用协议，或重写为当前确实需要且能被工具读取的协议。

### 4.3 活动 skill 有资源断链

- `paper-write/SKILL.md:53` 引用 `../shared-references/writing-principles.md`，文件不存在。
- `paper-write/SKILL.md:44` 指向“上面的 DATA_NEEDED 标记小节”，当前文档没有该小节；示例 HTML 注释也不适合作为可编译 TeX 占位符。
- 写作 skill 要求复制 templates/，但 `skill_resource_read` 只允许 `<skill>/references/`；仓库内没有 skill 自带 references/ 或 scripts/ 目录，skill_script_run 对现有内置 skill 无目标。普通 read 又不把 skill 根目录加入可读范围。bash 可通过绝对路径访问，但 skill 没有稳定、明确的模板获取接口。
- 建议统一只读资源工具来访问 skill 下的 references/templates，保留目录边界；现阶段无实际脚本的 skill_script_run 可先不在默认工具列表中展示。

### 4.4 可压缩的提示内容

- experiment-bridge 的 manifest 契约、run-experiment 子 skill、EXPERIMENT_INTERFACE、桥接节点 prompt 反复讲 block_id、depends_on、runner 生成 tracker、复用回执。schema 和执行语义可由运行器统一提供，skill 专注研究实施与结果解释。
- improvement skill 中的轮数、审稿模型、审稿配置和输出命名与 Python 控制逻辑重叠。运行参数由运行器提供，skill 保留修改策略和科学约束。
- `allowed-tools` frontmatter 由文本读入，运行器没有解析；真正权限在 profiles.py。部分名字还沿用 Read/Write/Glob，与本地小写工具不一致。建议只维护一份实际工具权限，skill 不再重复列权限。
- `idea-creator/SKILL.md:26` 一句中重复出现同一个 RESEARCH_REPORT.md；research-lit 的“六项核心字段”实际只列五项。可直接修正文案。
- TOOL_BOUNDARIES.md 的 ideas “runner pre-reads wiki and online metadata” 与当前 `_run_ideas` 让 agent 按需读取的行为不同；文末句子也不完整。project-evidence.md 仍提到 proposal 等旧节点术语。应按当前路由更新。

## 5. 不应误删的内容

- novelty skill：文档明确保留独立用途，可清理的是主工作流内不可达包装。
- arXiv 旧式分类 ID：它们是真实论文标识，规范化支持不是过期业务流程兼容。
- 图片接口失败后的代码绘图回退、OpenAlex 下载回退：属于当前故障处理。
- read_local_paper 的快速筛读与 MinerU 全文读取：用途不同，不是简单重复依赖。
- review_runner 的 CLI 路径发现与单独 review CLI：仍有当前业务调用。
- 写作、绘图、实验设计之间重复强调证据真实性：部分重复是职责边界；应优先压缩机械协议，而非盲目删除科学约束。

## 6. 建议处理顺序

1. 修活动 skill 的断链、模板接口，隔离测试的外部调用。
2. 移除 LangGraph/fonttools 的无用主依赖及启动导入，重锁并验证环境。
3. 归档 12 份旧稿、清理未接入的共享协议、旧文案与工具列表。
4. 为仍需恢复的 checkpoint/config/manifest 做一次性版本迁移。
5. 删除已迁移的兼容分支、无用途状态和不可达节点包装。
6. 合并节点元信息、产物契约、有效回执计算和 CLI 参数，最后精简重复 prompt。

## 7. 验证记录

静态核对了 import、方法引用、实际路由、资源加载、package-data、锁文件依赖图和主环境安装版本。没有启动 live 脚本，没有修改或重启已有研究任务。

首次执行完整 pytest 时，旧主流程测试没有 mock 新增 improvement 审稿调用，进入真实外部 CLI；已中断该测试进程，并通过其清理路径终止审稿子进程。第二次用不存在的 CODEX_BIN 隔离外部审稿后完成：**342 passed，3 failed，87.95 秒**。

| 失败测试 | 原因与判断 |
| --- | --- |
| `test_data_search.py:33` | 仍断言 idea prompt 包含数据报告全文；当前 `_run_ideas` 改为提供报告路径并要求 read 工具读取。这是交接方式变更后的旧断言，应验证实际读取行为 |
| `test_main_workflow.py:12` | 名称与断言仍要求无审稿、停在 paper_compile，但生产路由已进入 improvement；未 mock 外部审稿。隔离复跑因此失败，不能据此认定实际审稿功能不可用 |
| `test_tools.py:8` | 要求图表设计/审阅子阶段只有 find/read/grep；profiles 后续又给 design 加了图检索、读资源、看图工具。应先决定这些未被生产路由使用的子阶段是否保留，再更新或删除测试 |

隔离复跑命令：`CODEX_BIN=/nonexistent/paper-agent-audit-offline-codex /data/apps/micromamba/envs/xinghao/bin/python -m pytest -q --tb=short`。本次没有修复这些断言，避免把检查任务扩大成行为变更。

## 8. 后续清理结果

本节记录用户授权清理后的状态；前文是清理前的审计快照，文件行号和测试数量可能已变化。用户要求主流程 skill 暂不修改，因此本次没有修改任何 SKILL.md。

- 删除 langgraph、fonttools 的依赖声明及启动导入，离线重生成 uv.lock：锁定包从 56 个降至 33 个，保留包版本未变。没有卸载共享 Python 环境中的包。
- 删除不可达的旧 novelty 节点、实验执行包装及其专属校验/渲染代码；保留独立 novelty skill 入口。删除两个实验扩展状态字段，并在旧 checkpoint 加载时忽略它们。
- 删除 writing、paper_figure_design、paper_figure_review 三个闲置工具阶段；删除没有内置脚本目标的 skill_script_run 工具及注册。
- skill_resource_read 支持 references/ 和 templates/ 资源，保持裸文件名默认读取 references/ 的行为；补充绝对路径、目录穿越、文件和目录符号链接越界校验。写作模板已有可用的读取接口。
- 因主流程 skill 暂不改，writing-principles.md 缺失引用、DATA_NEEDED 不存在的小节引用等文字断链仍待处理；旧 skill 草稿和未接入的共享协议也保留。不能将这些项目视为已清理。
- 更新引用已删私有节点/阶段的测试，主流程测试显式在 paper_compile 停止，避免误触发真实审稿；报告交接断言更新为当前路径交接协议。

验证：完整测试 **338 passed，87.84 秒**；外部审稿以不存在的 CODEX_BIN 隔离。`uv lock --check --offline`、`bash -n run-workflow` 均通过，`./run-workflow --check-env` 的 Python、PyTorch 和 CUDA 实际计算检查通过。

旧节点映射、旧配置回退、旧回执恢复、缺省顺序依赖和 CLI 别名仍保留。这些兼容分支涉及已有项目的恢复语义，需要先迁移数据再删除。实验接口、计算策略、改稿参数和工具声明的提示词重复，本次仅解释问题，尚未调整主流程 skill 或统一注入方式。

## 9. 旧 checkpoint 兼容移除

根据用户后续授权，删除 run 恢复时的 experiment_check/run → experiment_bridge 和 novelty → refine 映射，同时删除 start_from_project 的旧实验节点别名。旧节点现在与其他未知节点一致被拒绝，不会自动改写执行位置。

WorkflowState.from_dict 不再过滤退役字段，直接按当前状态结构构造对象；旧字段与其他未知字段一样报错。当前结构的序列化/恢复保持不变。更新恢复测试，覆盖当前状态往返、全部已移除字段的拒绝，以及旧节点在恢复和显式入口两条路径上的拒绝。未修改主流程 SKILL.md 或已有项目 checkpoint；旧配置和回执等其他兼容逻辑仍保留。

验证：workflow、主流程、CLI、运行预算及实验桥接相关测试共 **68 passed**，外部审稿继续隔离。

## 10. 全部旧项目兼容清理

用户明确不再要求复用旧项目，因此移除运行时旧资源配置回退、旧回执恢复和重复写入、隐式顺序依赖、CLI 参数别名及 phase 别名。当前公开 workflow 的 refine 节点保留，内部 phase 为 research_refine。队列进度统一使用 experiment_bridge。

额外移除 skill 资源裸文件名补全、ZOTERO_USER_ID 环境变量回退、Wiki 独立脚本导入/缺少扫描模块时继续执行的兼容分支、旧编码专用处理，以及 scripts/migrate_wiki_schema.py。保留当前结构的 checkpoint 恢复、磁盘回执读取、故障回退及真实 arXiv 标识解析；这些服务于当前运行或外部数据，并非旧项目格式迁就。主流程 SKILL.md 和已有项目数据未修改。

运行器及其接口提示要求每个 job 显式声明 depends_on；主流程 skill 中旧的省略依赖行为说明按用户要求未改，运行语义以当前运行器为准。当前使用方式见 docs/runtime.md。新增/更新测试覆盖旧回执不再复用、不再重复写旧状态、当前阶段进度、旧参数及 phase 拒绝、缺失依赖拒绝、资源路径前缀和 Zotero 环境变量规则。

最终验证：完整测试 **352 passed，83.66 秒**，外部审稿以不存在的 CODEX_BIN 隔离。

## 11. 工作流提示词中文化与执行子 skill 去重

按用户要求，将 workflow.py 发给各阶段 Agent 的任务提示、上下文标签、审稿 instructions、执行工具说明和操作指引翻译为中文；工具名称、路径、参数和 JSON 字段保持原样。桥接节点直接拼接的 EXPERIMENT_INTERFACE 也翻译为中文，保留原有协议和示例。未改写 loop.py 的全局 system prompt。

run-experiment 子 skill 删除重复的 manifest 字段、依赖协议、资源策略、tracker 归属、回执复用和结果报告要求，保留实际输入检查、日志定位、故障分类与交回 bridge 的指引。保留子 skill 入口及 bridge 中的调用，不修改其他主流程 SKILL.md。

EXPERIMENT_INTERFACE 的注入发生在 _run_experiment_bridge 调用 _go 时，位于初始 user prompt 末尾；只在进入或重新进入该节点时拼接。同一次工具循环中的后续模型请求携带原有历史，不会每次追加一份。

验证：完整测试 **352 passed，83.63 秒**；run-experiment 的 skill 格式校验通过。外部审稿继续隔离。

## 12. 删除计划与数据报告上下文注入方法

按用户要求，删除 WorkflowRunner._inspect_experiment_plan 和 _data_report_context 方法及所有调用点，不留兼容入口。bridge 不再注入实验计划快照和数据报告全文；refine 和 experiment_plan 也不再通过该方法注入数据报告。已有报告文件、计划文件及其正常校验保留，ideas 阶段的报告读取要求不变。更新下游报告交接测试，验证数据报告全文不再自动嵌入提示词。主流程 skill 保持不变。

验证：workflow、数据交接、实验桥接、执行协议及修复相关测试 **77 passed**；代码和测试中无这两个方法的残留引用。

## 13. 删除 bridge 初始提示词中的已保存反馈

按用户要求，删除 bridge 初始提示词中的“已保存的执行反馈”及其局部字典构造。保留 experiment_feedback 状态和 protected_evidence 校验，节点内部仍通过工具返回错误供模型修复、重试。主流程 skill 保持不变。

验证：实验桥接及工作流修复相关测试 **23 passed，45.59 秒**，外部审稿继续隔离。

## 14. 统一产物校验并修复 tracker 完成状态

demo-project21 的 duplicates.json 为合法空数组，却被刷新校验认定无效，继而使下游完成标记失效。JSON 对象/数组及 CSV 表现在默认允许空集合，文件本身仍必须存在且非零字节；expected_outputs 支持布尔字段 nonempty，显式要求非空。metrics 的字段存在性、有限数值校验保留。

执行器在进程成功退出后使用同一产物校验，通过后才写完成回执；不通过写失败回执并通过工具错误进入 bridge 内部修复，下游任务不会放行。队列展示产物校验失败并保留进程成功退出事实。tracker 刷新依据最新匹配回执和当前产物，区分产物校验失败、依赖产物无效、进程失败和未执行，新增进程成功退出计数与任务问题明细，避免把已执行任务直接显示为待运行。运行器注入的接口及 runtime 文档同步更新，主流程 skill 未改。

仅重新校验并刷新 demo-project21 的 EXPERIMENT_TRACKER.md，52 个任务产物全部通过，8 个 D/B 块均显示完成。没有重跑实验或改写运行中的 workflow state。

验证：完整测试 **362 passed，89.84 秒**，包含空集合、显式非空、非法 JSON、缺失产物失败修复、下游阻塞、产物失效及恢复后 tracker 重建；外部审稿继续隔离。

## 15. 删除 data_search 节点的额外提示与文献全文注入

按用户要求，删除节点提示中资源状态区分、禁止模型训练的指令，以及带“不可信来源数据”标签的文献报告全文注入；清理不再使用的文献正文变量，并删除此前编辑后留下的不完整“除样本数据外”短句。保留文献输入文件校验、来源路径元数据及登记数据源信息。共享证据文件的自动注入位置已解释，本次未修改该机制或主流程 skill。

验证：数据检索交接相关测试 **6 passed**。

## 16. 删除共享证据文档及自动注入

按用户要求，删除 shared-references/project-evidence.md，并删除 loop.py 的 _skill_prompt 中读取、拼接该文档的代码；各阶段不再自动注入共享证据合约。实际注入入口位于 PhaseAgent，而非 workflow.py。同步清理 TOOL_BOUNDARIES.md 的加载说明；主流程 SKILL.md 未修改，其他共享文档保留。

验证：数据检索、idea 及阶段 Agent 相关测试 **29 passed**；运行代码和测试中无 project-evidence 引用。

## 17. Figure 1 按最终交付路径验收

取消 Figure 1 的硬编码 PNG 文件名。节点提示要求在 figures/figure_manifest.json 的 figure_1 对象声明 asset 和 preview，生成图二者指向实际输出的同一 PNG；代码回退使用 PDF asset 和 PNG preview，回退清单须保持一致。运行器核对最终 LaTeX 引用、当前视觉检查、当前调用成功回执和文件哈希，仅校验实际交付生成图，同路径重生成取最新回执，未使用草稿不阻塞交付。缺声明、未引用和缺对应生成回执分别报错。回退工具指引及 runtime 文档同步更新，主流程 skill 与运行中的项目文件未改。

验证：图工作流及图生成工具测试 **28 passed**。完整测试 **364 passed、1 failed，89.92 秒**；唯一失败为 tests/test_data_search.py::test_literature_data_search_ideas_handoff，旧断言要求 ideas prompt 包含 data-search/DATA_REPORT.md，但当前节点提示已不包含该路径，与此次 Figure 1 修改无关。

## 18. 区分项目研究目标与节点补充指令

移除显式节点入口对 prompt 必须与保存任务完全相同的限制。首次 prompt 保存在 task_prompt；已有目标不被覆盖，不同的新 prompt 保存为 node_instructions，与原始研究目标一起传给本次入口节点，后续自动路由节点仅接收原始目标。节点结果和 history 记录实际使用的补充指令，新的调用不会自动继承上次指令。runtime 文档增加只运行 paper_figure 的 CLI 示例；未修改已有项目状态或主流程 skill。

验证：工作流、CLI 和修复相关测试 **58 passed，38.78 秒**，覆盖不同 prompt 恢复、原始目标保持、入口节点指令隔离、状态序列化、再次运行清除旧指令及首次显式入口。

## 19. 补齐 CLI 停止节点参数

修复此前示例使用内部 stop_after 但 CLI 未提供 --stop-after 的问题。CLI 新增 --stop-after NODE；--single-node NODE 同时设置入口与停止节点，以恢复已有项目状态并只运行目标节点。组合冲突参数明确报错。没有启动真实工作流。

验证：CLI 测试 **11 passed**，覆盖 --start-node/--stop-after、--single-node 和冲突拒绝。

## 20. literature 专家检查点

新增 --mode auto/expert，默认 auto；expert 仅在 literature 完成后进入持久化 awaiting_user 状态并正常退出。literature 在同次阶段调用中根据报告生成 EXPERT_CHECKPOINT.json 的摘要和 2–3 个问题，完成校验拒绝缺失或无效结构；修订前删除旧检查点文件，避免旧问题冒充本次输出。终端显示摘要、问题、可编辑报告和继续方式。

新增 --continue，重新读取用户修改后的报告，记录确认哈希和历史，从 data_search 继续，不重跑 literature。新增 --revise，让暂停节点按意见修订并再次暂停，原始 task_prompt 保持不变。保存模式及 stop_after，恢复不要求重复 prompt；空报告不能确认，无待审阅状态不能恢复。主流程 skill 和真实项目文件未修改，没有启动真实模型或实验任务。

验证：专家检查点、CLI、workflow 测试 **63 passed**；加入 data_search 测试后 **68 passed、1 failed**，唯一失败仍为此前已有的 test_literature_data_search_ideas_handoff 对已删除 ideas prompt 路径的旧断言。CLI --help 已确认新参数可用。

## 21. 专家模式终端连续交互

修正专家模式每次暂停都退出、必须手动恢复的问题。交互终端在 literature 检查点等待输入，普通回答连同问题写入报告后自动确认并进入下一节点；/continue 接受用户自行编辑后的报告，/revise 意见 修订后重新审阅，/quit、Ctrl+C 和 EOF 保留待确认状态退出。非交互环境保持持久化暂停，支持已有 --continue/--revise 手动恢复。后续调用保留本次模型、预算和超时选项，停止位置从检查点恢复。

验证：专家检查点、CLI、workflow 测试 **69 passed**，覆盖回答写入文件和确认哈希、自动下游执行、修订后交互循环、手动文件确认、无终端及退出恢复。未调用真实模型或修改主流程 skill。

## 22. 用户回答先修订正文，再自动进入下游

专家检查点普通回答先保存，再作为 literature 修订指令，要求整合研究范围、任务边界、模型建议和验证方案，消除与用户选择冲突的正文，保留有效证据与用户回答。修订成功后自动确认当前报告并继续 data_search，不再重复询问；修订失败不进入下游。空白回车接受当前报告直接继续，不修订。显式 /revise 仍保留修订后人工复核，EOF/Ctrl+C 仍保存退出；不设置无人输入自动超时。

验证：专家检查点、CLI、workflow 测试 **71 passed**，覆盖回答修订后下游读取新正文及哈希、空白回车不修订、修订失败阻断下游。未修改正在运行的 demo-project22 或主流程 skill。

## 23. 统一专家审阅入口

移除面向用户的 --continue/--revise 参数与所有终端斜杠命令。检查点仅接受回答或回车：有回答先修订再自动继续；直接编辑报告后回车，读取最新文件直接继续；空回车接受当前报告。Ctrl+C/EOF 保存退出，使用 --project 项目目录、不提供 prompt，即可回到相同审阅入口。内部修订、确认执行机制保留，用户无需选择操作命令。runtime 文档更新为统一交互方式。

验证：专家检查点、CLI、workflow 测试 **71 passed**，覆盖直接编辑文件后进入下游、无 prompt 恢复审阅、移除的 CLI 参数拒绝、回答修订及失败阻断。未修改运行中项目或主流程 skill。

## 24. 所有节点统一专家审阅

将专家检查点扩展至全部 11 个 workflow 节点。在公共 _go 注入本节点产物路径及摘要/问题输出要求，完成校验保证本次检查点有效；无模型调用的成功编译节点补充仅审阅产物的调用。各节点检查点使用独立文件名。CLI 展示节点名和主要产物，回答交给当前节点修订后自动确认并进入下一检查点；手动修改后回车读取磁盘最新版，ideas 的内存内容同步更新。最后节点和 stop_after 节点先审阅再完成，不重跑已确认节点。

确认时检查主要文件并记录哈希；实验计划刷新 tracker，实验桥接重查 manifest、执行回执、产物和依赖，变更或无效任务阻断下游；paper_compile/improvement 确认重新编译当前源码。paper_compile 补充回答驱动的源码修订调用，并开放 write 以生成审阅 JSON。主流程 skill 和 demo 文件未修改。

验证：专家检查点、CLI、workflow、工具测试 **117 passed**；补充检查点节点一致性约束后专家测试 **46 passed**。覆盖所有节点暂停/编辑确认/下一节点/停止节点/末节点、非 literature 回答修订、实验变更阻断和论文重新编译路径。未运行真实模型、训练或图生成。

## 25. 临时模型连接错误重试

ModelClient 对连接错误、超时、HTTP/2 上游断流、流结束标记缺失、HTTP 408/409/429/5xx 提供首次失败后的 300 秒重试窗口，指数退避最高 15 秒，重试沿用当前 messages，未完成的流及工具调用不会执行。窗口剩余时间限制每次请求的超时，并在读取流时检查截止时间。自建 OpenAI SDK client 禁用内建重试以避免嵌套；非临时错误不重试。仅适用于 workflow 模型调用，已运行进程需重启。

验证：模型重试、paper agent、bridge orchestration、workflow repairs **56 passed**。测试覆盖断线分类、窗口耗尽、非临时错误、断流丢弃和已完成工具结果随原对话保留。未发起真实模型请求。
