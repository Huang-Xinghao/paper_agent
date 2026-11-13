---
name: experiment-bridge
description: "工作流程1.5：想法发现和自动审稿之间的桥梁。读取 EXPERIMENT_PLAN.md ，实现实验代码，部署到 GPU ，收集初始结果。当用户说“实现实验”、“实施实验”、“桥梁”、“从计划到运行实验”、“部署计划”或准备好执行实验计划时使用。"
argument-hint: "[experiment-plan-path-or-topic]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, Skill, mcp__codex__codex, mcp__codex__codex-reply
---

# 工作流程 1.5：实验桥

根据计划实施和部署实验：**$ARGUMENTS**

## 概述

该技能连接了工作流程1（想法发现+方法细化）和工作流程2（自动审稿循环）。它采用实验计划并将其转化为具有初步结果的运行实验。

```
工作流程 1 输出： 该技能： 工作流程 2 输入：
refine-logs/EXPERIMENT_PLAN.md → 实施 → GPT-6-Astra 审稿 → 部署 → 收集 → 初步结果准备就绪
refine-logs/EXPERIMENT_TRACKER.md 代码（跨型号）/run-experiment for /auto-review-loop
refine-logs/FINAL_PROPOSAL.md
```

## 常量

- **CODE_REVIEW = true** — GPT-6-Astra xhigh部署前的审稿实验代码。在浪费 GPU 小时之前捕获逻辑错误。设置 `false` 跳过。
- **AUTO_DEPLOY = true** — 实施+审稿后自动部署实验。设置 `false` 在部署之前手动检查代码。
- **SANITY_FIRST = true** — 在启动其余实验之前，首先运行基本正确阶段实验（最小、最快）。及早发现设置错误。
- **MAX_PARALLEL_RUNS = 4** — 并行部署的最大实验数量（受可用 GPU 的限制）。
- **BASE_REPO = false** — 用作基本代码库的 GitHub 存储库 URL。设置后，首先克隆存储库并在其上实施实验。当 `false`（默认）时，从头开始编写代码或重用现有项目文件。
- **COMPACT = false** — 当 `true` 时，(1) 读取 `idea-stage/IDEA_CANDIDATES.md` 而不是完整的 `idea-stage/IDEA_REPORT.md`（如果可用），(2) 在收集后将实验结果附加到 `EXPERIMENT_LOG.md`。

> 覆盖：`/experiment-bridge "EXPERIMENT_PLAN.md" — compact: true, base repo: https://github.com/org/project`

## 输入

此技能需要以下一项或多项：

1. **`refine-logs/EXPERIMENT_PLAN.md`**（最佳）——以论断为`/experiment-plan`的导向实验路线图
2. **`refine-logs/EXPERIMENT_TRACKER.md`** — 逐次运行执行表
3. **`refine-logs/FINAL_PROPOSAL.md`** — 实现上下文的方法描述
4. **`idea-stage/IDEA_CANDIDATES.md`** - 紧凑的想法摘要（当 `COMPACT: true` 时首选）*（如果未找到，则回退到 `./IDEA_CANDIDATES.md`）*
5. **`idea-stage/IDEA_REPORT.md`** — 完整的头脑风暴输出 *（如果未找到，则返回到 `./IDEA_REPORT.md`）*

如果不存在，询问用户要实施什么实验。

## 工作流程

### 第一阶段：解析实验计划

读取`EXPERIMENT_PLAN.md`并提取：

1. **运行顺序和里程碑** - 首先进行实验运行（基本正确性→基线→主要→消融实验→抛光）
2. **对于每个实验块：**
   - 数据集/分割/任务
   - 比较系统和变体
   - 要计算的指标
   - 设置详细信息（主干、超参数、种子）
   - 成功标准
   - 优先级（必须执行 vs NICE-TO-HAVE）
3. **计算预算** — 预计总 GPU 小时
4. **方法详细信息** 来自 `FINAL_PROPOSAL.md` — 到底要实现什么

做一个简短的总结：

```
📋 实验计划已加载：
- 里程碑：[N]（基本正确性→基线→主要→消融实验）
- 必须执行实验：[N]
- 最好有：[N]
- 预计 GPU 小时：[X]

进入实施阶段。
```

**研究合同后备**：如果 `idea-stage/docs/research_contract.md` 尚不存在（在 `/idea-discovery` 之外选择的想法，或较旧的运行），现在使用所选想法 + 实验计划中的论断从 `templates/RESEARCH_CONTRACT_TEMPLATE.md` 创建它。下游 `/result-to-claim` 和 `/ablation-planner` 读取此文件作为论断源，并且会话恢复（ `docs/SESSION_RECOVERY_GUIDE.md` ）依赖于它的存在。

### 第 2 阶段：实施实验代码

**如果设置了 `BASE_REPO`** — 首先克隆存储库：
```bash
git clone <BASE_REPO> base_repo/
# 阅读存储库的自述文件，了解其结构，找到入口点
# 通过修改或扩展此代码库实现实验
```

对于每个里程碑（按顺序），编写实验脚本：

1. **检查现有代码** — 扫描项目（或克隆的 `base_repo/` ）以查找现有实验脚本、模型代码、数据加载器。尽可能重复利用。

2. **实施缺失的部分：**
   - 具有适当 argparse 的训练脚本（所有超参数均可配置）
   - 计算指定指标的评估脚本
   - 如果需要，数据加载/预处理
   - 基线实现（如果尚未存在）
   - 修复随机种子以提高可重复性
   - 结果保存到 JSON/CSV 以供以后分析
   - 正确的日志记录（wandb 如果在 CLAUDE.md 中配置）

3. **遵循计划的运行顺序** — 首先实施基本正确阶段实验，然后是基线，然后是主要方法，然后是消融实验。

4. **部署前的自审稿：**
   - EXPERIMENT_PLAN.md 中的所有超参数是否都反映在 argparse 中？
   - 随机种子是固定可控的吗？
   - 结果是否以可解析的格式保存（JSON/CSV）？
   - 代码与 FINAL_PROPOSAL.md 的方法描述匹配吗？

### 阶段2.5：跨模型代码审稿（当CODE_REVIEW = true时）

**如果 `CODE_REVIEW` 为 `false` 则跳过此步骤。**

部署前，将实验代码发送至GPT-6-Astra xhigh进行审稿：

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    审稿执行以下实验以确保正确性。

    ## 实验计划：
    [粘贴 EXPERIMENT_PLAN.md 中的关键部分]

    ## 方法说明：
    [粘贴自FINAL_PROPOSAL.md]

    ## 执行：
    【粘贴实验脚本】

    检查：
    1. 代码是否正确实现了提案中描述的方法？
    2. 计划中的所有超参数是否都反映在代码中？
    3. 是否存在任何逻辑错误（错误的损失函数、错误的数据分割、缺少评估）？
    4. 评估指标计算是否正确？
    5. **关键：评估是否使用数据集的实际真实标签 - 而不是另一个模型的输出作为真实标签？** 这是一个常见且严重的错误。
    6. 是否存在任何潜在问题（OOM 风险、数值不稳定、种子缺失）？

    对于发现的每个问题，请指定：关键/主要/次要以及确切的修复方法。

    === 范围限制（这些范围限制了您的提议，而不是您所寻找的内容）===
    报告任何实际错误的情况 - 包括看起来罕见的案例，如果
    这个仓库实际上产生了它。然后将修复保持在范围内：
    1. 这是一个研究工作流程工具，而不是安全论文。验证是
       欢迎;过度防御则不然。假设有自己的合作运营商
       机器 - 恶意本地用户不在威胁模型中。
    2. 不要建议 SHA / 散列 / 内容指纹 / 摘要绑定方案。
       报告散列代码中已经存在的真正缺陷是可以的。
    3. 无投机机制：不添加功能标志、迁移框架、
       兼容层、包装器、引脚或类似机制，除非有证据表明
       他们修复的当前回购缺陷或他们必须修复的明确现有不变量
       保存。 “承重”、“兼容”、“不搭脚手架”都是标签，
       不是证据。指向失败的 path/artifact 或不变量，并检查
       提案的事实前提，例如是否存在指定的包版本。
    4. 没有极端情况的困扰：外来编码、符号链接竞争、RTL 文本和
       除非您能证明此处出现了这种情况，否则毫秒竞赛超出了范围。
    5. 当真正需要一个标题或清单时，不要过度机械化
       判断。人类读出的清晰句子胜过评分表
       维持。
    例外：运行远程命令、启动网络服务或安装的代码
    MCP 服务器使用其凭据在用户计算机上运行 — 信任边界
    调查结果在范围内并且默认是严格的。
    当某些事情是正确的时，请直白地说出来。不要制造调查结果。
```

**关于审稿结果：**
- **没有严重问题** → 进入第 3 阶段
- **发现关键问题**→修复，然后重新提交审稿（最多2轮）
- **Codex MCP 不可用** → 静默跳过，进入第 3 阶段（优雅降级）

### 第 3 阶段：基本正确性检查（如果 SANITY_FIRST = true）

在部署完整的实验集合之前，运行基本正确性阶段的实验：

```
/run-experiment [基本正确性实验命令]
```

等待完成。核实：
- 训练循环运行没有错误
- 指标计算正确并保存
- GPU 内存使用量在范围内
- 输出格式符合预期

如果基本正确性失败 → **放弃之前自动调试**。预算：针对同一故障最多 **2 次补丁尝试**，然后最多 **2 次干净的重新实现**（总共 4 次）：

1. **读取错误** — 解析回溯、stderr 和日志文件。 （同样的阅读主要产物规则也适用于令人惊讶的审稿人判决：参见 `shared-references/review-tracing.md` § *调试跟踪*。）
2. **诊断** — 对故障进行分类：
   - OOM → 减少批量大小或启用梯度检查点
   - 导入错误→安装缺少的包
   - FileNotFoundError → 修复路径或下载数据
   - CUDA错误→检查GPU可用性，减小模型大小
   - NaN/divergence → 降低学习率，检查数据预处理
3. **修复并重新运行** — 应用修复，重新运行基本正确性
4. **尝试 2+ 仍然失败？ → 调用Codex救援**（如果安装了Codex插件）：在下次重试之前，调用`/codex:rescue`以获得关于根本原因的第二意见。  Codex 独立读取代码和错误日志 - 它可能会发现 Claude 遗漏的问题（错误的张量形状、微妙的导入阴影、配置不匹配等）。应用建议的修复，然后重新运行。
   - 如果 `/codex:rescue` 不可用（未安装插件），则继续 Claude 自己的诊断
5. **两次补丁尝试都因同一故障而失败？ → 彻底丢弃并重新实施**（最多 2 次重新实施）。从 `EXPERIMENT_PLAN.md` / 研究合同重写失败的脚本是同行移动到另一个补丁，而不是最后的手段 - 在两个错误的补丁之上的第三个补丁通常比干净的重建更糟糕。仅删除尝试自己的 code/scaffolding（此阶段生成的脚本）；该计划、 `EXPERIMENT_TRACKER.md` 、用户创作的项目源、收集的数据和结果永远不可删除（请参阅 `shared-references/external-cadence.md` § *让失败的尝试重新启动，而不仅仅是修补*）。
6. **预算耗尽（2 个补丁 + 2 个重新实现），或者两个重新实现以同样的方式失败？** → 停止，报告失败以及所有尝试的修复和错误日志。两个干净的重新实现相同地失败通常意味着计划或环境是错误的——在报告中明确地说，因为这（而不是损坏的构建本身）才是需要人类的。不要继续使用损坏的代码。

> 永远不要因为第一次失败而放弃。大多数实验崩溃无需人工干预即可修复。

### 第 4 阶段：部署完整实验

按照计划的里程碑顺序部署实验。 **按工作数量划分的路线**：

**小批量（每个里程碑≤5 个作业）** → 直接使用 `/run-experiment`：
```
/run-experiment【实验命令】
```

**大批量（≥10 个作业、多种子扫描或阶段依赖性）** → 使用 `/experiment-queue` 进行正确的编排：
```
/experiment-queue [网格规范或清单]
```

自动路由规则：如果 `EXPERIMENT_PLAN.md` 中的任何里程碑声明 ≥10 个作业（例如， `seeds: [42, 200, 201, ...]` × `N: [64, 128, 256]` × `n: [50K, 150K, 500K, 652K]` = 36 个作业）或声明教师→学生阶段依赖性，则将该里程碑路由到 `/experiment-queue` 。否则使用 `/run-experiment` 。

`/experiment-queue` 增加了：带有退避功能的 OOM 感知重试、陈旧 screen 清理、波转换竞争预防、相位依赖强制执行、`queue_state.json` 中的崩溃安全状态持久性。有关清单 YAML 格式，请参阅 `skills/experiment-queue/SKILL.md`。

对于每个里程碑：
1. 并行部署实验（对于 `/run-experiment` 最多为 MAX_PARALLEL_RUNS ，对于 `/experiment-queue` 最多为清单中的 `max_parallel` ）
2. 使用 `/monitor-experiment` 跟踪进度（如果 `/experiment-queue` 处于活动状态，则从 queue_state.json 读取）
3. 实验完成后收集结果

**🚦 检查点（如果 AUTO_DEPLOY = false）：**

```
🔧 代码实现完成。准备部署：

里程碑 0（基本正确性）：[状态 — passed/pending ]
里程碑 1（基线）：[N 次实验，~X GPU 小时]
里程碑 2（主要方法）：[N 次实验，~X GPU -小时]
里程碑 3（消融实验）：[N 个实验，〜X GPU -小时]

估计总计：[N] 个 GPU 上的 ~X GPU 小时

现在部署？还是审稿代码先？
```

### 第五阶段：收集初步结果

实验完成后：

1. **解析输出文件** ( JSON/CSV/logs ) 以获得关键指标
2. **训练质量检查** - 如果 W&B 数据可用（ CLAUDE.md 具有 `wandb: true` 和 `wandb_project` ），则调用 `/training-check` 来检测 NaN、损失散度、平稳或过度拟合。如果未配置W&B，则静默跳过。
3. **更新 `refine-logs/EXPERIMENT_TRACKER.md`** — 填写状态和注释列
4. **检查 EXPERIMENT_PLAN.md 的成功标准** — 每个实验是否都达到了标准？
4. **写出初步结果摘要：**

```markdown
# 初步实验结果

**日期**：[今天]
**计划**：refine-logs/EXPERIMENT_PLAN.md

## 按里程碑划分的结果

### M0：基本正确性 — 通过
- [结果]

### M1：基线
| 运行 | 系统 | 关键指标 | 地位 |
|-----|--------|-----------|--------|
| R001 | 基线_1 | XX | 完毕 |

### M2：主要方法
| 运行 | 系统 | 关键指标 | 地位 |
|-----|--------|-----------|--------|
| R003 | 我们的方法 | XX | 完毕 |

### M3: 消融实验
...

## 概括
- [ X/Y ] 必须执行实验已完成
- 主要结果：[ positive/negative/inconclusive ]
- 准备好 /auto-review-loop ：[ YES/NO ]

## 下一步
→ /auto-review-loop“[主题]”
```

### 阶段 5.5：写入紧凑日志（当 COMPACT = true 时）

**如果 `COMPACT` 是 `false` 则完全跳过。**

将每个已完成的实验附加到 `EXPERIMENT_LOG.md` ：

```markdown
## [运行ID] — [时间戳]
- **系统**：[方法名称]
- **配置**：[关键超参数]
- **结果**：[主要指标 = X.XX]
- **结论**：[正面/负面/不确定]
- **复制**：`python train.py --config configs/run_id.yaml --seed 42`
```

该结构化日志在会话恢复后仍然存在 - 下游技能读取它而不是解析 screen 输出。

### 5.6阶段：自动消融实验规划

当主要实验（M2）完成并取得积极结果后，调用`/ablation-planner`设计消融实验研究：

- 阅读主要结果和方法说明
- 生成一个以论断为导向的消融实验计划：要删除哪些组件、要比较什么、预期结果
- 将消融实验块附加到`refine-logs/EXPERIMENT_PLAN.md`和`refine-logs/EXPERIMENT_TRACKER.md`
- 如果主要结果是否定的或不确定，请跳过消融实验计划并在摘要中注明

如果`/ablation-planner`不可用，则静默跳过——现有的EXPERIMENT_PLAN.md消融实验块（如果有）保持不变。

### 阶段 6：交接

目前最终状态：

```
🔬 实验桥完成：
- 已实施：[N] 个实验脚本
- 已部署：在 [M] 个 GPU 上进行 [N] 次实验
- 已完成: [ X/Y ] 必须执行, [ A/B ] 很好
- 主要结果：[一句话]

结果：refine-logs/EXPERIMENT_RESULTS.md
追踪器：refine-logs/EXPERIMENT_TRACKER.md

准备工作流程 2：
→ /auto-review-loop“[主题]”
```

## 输出协议

> 所有输出文件均遵循以下共享协议：
> - **[输出版本控制协议](../shared-references/output-versioning.md)** — 首先写入带时间戳的文件，然后复制到固定名称
> - **[输出清单协议](../shared-references/output-manifest.md)** — 将每个输出记录到 MANIFEST.md
> - **[输出语言协议](../shared-references/output-language.md)** — 尊重项目的语言设置

## 主要规则

- **关键 — 评估必须使用数据集真实标签。** 编写评估脚本时，始终将模型预测与数据集的实际真实标签 labels/targets 进行比较 — 切勿使用其他模型的输出作为真实标签。仔细检查：(1) 真实标签来自数据集分割，而不是来自 baseline/backbone 模型，(2) 评估指标是根据所有方法的相同真实标签计算的，(3) 如果任务有官方评估脚本，请使用这些脚本。
- **按照计划进行。** 不要发明不在EXPERIMENT_PLAN.md中的实验。如果您认为缺少某些内容，请记下但不要添加。
- **首先是基本正确性。** 在未验证基本正确性阶段通行证的情况下，切勿部署全套套件。
- **重用现有代码。** 在编写新脚本之前扫描项目。扩展，不要重复。
- **将所有内容保存为JSON/CSV.** 自动审稿循环需要可解析的结果，而不仅仅是终端输出。
- **更新跟踪器。** `EXPERIMENT_TRACKER.md` 应在每次运行完成后反映真实状态。
- **不要永远等待。** 如果实验超过了预计时间的 2 倍，请对其进行标记并继续进行下一个里程碑。
- **预算意识。** 根据计划预算跟踪 GPU 小时。如果接近极限则发出警告。
- **Vast.ai 生命周期。** 如果使用 vast.ai 实例，请在所有实验完成并下载结果后销毁它们。运行实例每秒都会产生费用——不要让它们闲置。完成后使用 `/vast-gpu destroy` 或 `/vast-gpu destroy-all`。
- **Modal 生命周期。** 如果使用 `gpu: modal` ，则无需清理 - Modal 在每次运行后自动缩放为零。但在运行之前始终显示成本估算，并验证支出限制是否设置为 https://modal.com/settings（切勿通过 CLI）。

## 与其他技能组合

```
/idea-discovery“方向”←工作流程1：寻找+细化+计划
/experiment-bridge ← 你在这里（工作流程 1.5：实施 + 部署）
/auto-review-loop“话题”←工作流程2：审稿+迭代
/paper-writing " NARRATIVE_REPORT.md " ← 工作流程 3：写论文

或者使用 /research-pipeline 实现完整的端到端流程（包括此桥）。
```

