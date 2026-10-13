---
name: paper-figure
description: 根据论文计划和真实电池实验结果，固定生成四张可复现、可审计的论文主图。
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, generate_image, search_wiki, read_wiki
---

# 论文图表绘制

读取 `PAPER_PLAN.md`、`refine-logs/FINAL_PROPOSAL.md`、`refine-logs/EXPERIMENT_PLAN.md`、
`experiment-runs/` 中的执行收据 和 `results/` 中的真实结果，围绕论文故事固定生成四张主图或多张子图。
本 skill 负责图稿规划、数据读取、绘图脚本、渲染和视觉检查，不重新设计实验、不启动训练、不补造数据或数字。

开始规划或绘图前，必须先读取项目共享指南：
`电池资源库/共享指南/电池论文图表通用设计指南.md`。
共享指南是本 skill 的通用设计基线，必须用于确定图表论证目标、图1/图2/结果图的组织方式、点线面表达、颜色和字体语义、图标使用、证据边界以及导出后的视觉检查。若该文件不存在或无法读取，在 `figure_plan.md` 中记录缺口，并继续遵守本 skill 已明确的证据和可复现要求，不得自行补写共享指南内容。

两份规范的职责分工如下：本 skill 固定四张主图的职责、输入优先级、文件命名和交付物；共享指南提供跨项目可复用的设计细节。若具体项目证据不足，优先保留缺口并减少子图，不得为满足版式或图标丰富度虚构数据。图标只用于场景、流程和面板语义提示，不能编码数值、模型优劣或显著性，也不能遮挡数据。

## 固定四张主图

| 图号 | 固定职责 | 输入优先级 | 生成方式 |
|---|---|---|---|
| 图1 | 研究场景、问题动机与实际模型架构 | FINAL_PROPOSAL、论文计划、实际模型配置/代码 | 调用 image 接口生成主图；必要时用代码叠加准确文字 |
| 图2 | 数据集读取结果与特征依据 | 实验 1.x 的 CSV/JSON 和数据说明 | Python/Matplotlib 或项目绘图资源代码 |
| 图3 | 总体性能、跨数据集结果、基线对比和消融 | 实验 2.x、3.x、4.x 的真实结果 | Python/Matplotlib；agent 自主设计子图 |
| 图4 | 不确定性、稳健性、失败边界和补充诊断 | 实验 5.x 及其他已完成诊断结果 | Python/Matplotlib；agent 自主设计子图 |

用户若把总体性能、对比、消融、不确定性分别称为“图3/图4/图5”等结果方向，仍须映射到上述固定四张主图；
不得因此自动增加主图数量。每张主图中的子图数量、布局和具体图型由 agent 根据证据自主决定，但必须说明每个子图承担的判断任务。

## 各图接受的输入

### 图1：动机与架构

至少读取：

- `PAPER_PLAN.md` 中的问题、主张、方法和图表计划；
- `refine-logs/FINAL_PROPOSAL.md` 中的研究场景、任务和核心机制；
- 实际实验包中的模型入口、配置、特征定义、窗口、标签和推理流程；
- 若存在，`results/` 中用于说明预测起点、输入边界和输出形式的元数据。

必须准确表达：电池/材料体系、观测窗口、预测起点、输入特征、核心模块、信息流、RUL/SOH/SOC 或轨迹输出、EOL 关系。
不得绘制代码中不存在的模块、未来真实标签或未经验证的性能提升。图1采用上下两层 `(a) Motivation`、`(b) Architecture`，
保持术语和颜色语义与图2–4一致。调用 image 接口时保存完整 prompt、模型、参数、返回状态和修订记录；接口不可用时只交付设计和待生成状态。

### 图2：数据集与输入特征

优先读取实验 1.x 的产物，典型输入包括但不限于数据集分析产出的数据：

- 原始退化轨迹 CSV；
- 容量差异 CSV；
- 充放电曲线差异 CSV；
- 模型输入特征 CSV；


agent 根据真实字段自主选择能形成完整数据故事的子图，例如退化轨迹、容量分布、充放电曲线、工况覆盖、特征分布或特征与标签关系。


### 图3：总体性能、对比与消融

优先读取：

- 实验 2.x 的各数据集、split、预测窗口和工况结果；
- 实验 3.x 的基线/模型统一评测结果、配置和资源对照；
- 实验 4.x 的模块变体、逐实体结果和消融汇总；

agent 自主设计子图组合，但至少应覆盖主张所需的总体结果和一个归因或公平对比视角；可选预测-真实轨迹、误差分布、分组性能、基线排序、
消融贡献、参数/资源权衡等。不同数据集或容量基准不可无说明直接合并；统计区间必须有重复或实体级依据。

### 图4：不确定性与边界

优先读取实验 5.x 的 2–3 个不确定性子实验及真实输出，结合已有结果中的跨工况、时间外推、噪声、失败案例或 OOD 诊断。
agent 根据实验实际设计选择预测区间覆盖、校准、风险分组、可靠性、分布外检测或失败边界等子图，不预设某一种方法。
每个区间必须注明含义、置信/预测水平、聚合单位和样本数；不确定性结果不可与点预测误差混为同一指标。

## 统一样式与可复现要求

- 图内标签、坐标轴、图例和图注默认使用英文，字体统一 Arial；缺少 Arial 时明确报告，不静默替换最终字体。
- 默认白底、深灰文字和低饱和蓝橙主色；同一方法、数据集、基线、模块和不确定性语义在四图中颜色固定。
- 使用 Matplotlib/Pandas/NumPy 绘制图2–4；优先导出 PDF 矢量图和至少 300 dpi PNG，设置 `pdf.fonttype=42`、`ps.fonttype=42`。
- 固定排序、筛选规则、随机种子和输出路径；绘图脚本从项目根目录运行，可重复覆盖同一输出。
- 图型由数据和问题决定，可以使用折线、散点、箱线/小提琴、柱图、热图、可靠性图或多面板组合；不得为了填满子图重复同一结论。
- 每个子图标明单位、方法、数据集/工况、样本量和区间含义；历史输入、未来真值、预测和示意曲线视觉上区分。
- 不修改原始 CSV/JSON；所有派生筛选和聚合写入脚本或图注记录。

## 图注解与正文叙事接口

绘图完成后，生成一个 `figures/figure_captions.md`。agent 在 `figure_plan.md` 中计划生成几张图，该文件就按图号生成几个章节，例如 `## Figure 1`、`## Figure 2`。每个章节只写该图在论文中使用的正式图注，可以直接作为 LaTeX `caption` 的内容；不需要额外的故事分析、证据报告或正文写作指导。



## 必须产出

在项目根目录 `figures/` 下固定写入：

- `figure_1_motivation_architecture.png`（必要时同时保存 PDF、prompt 和候选版本）；
- `figure_2_dataset_features.pdf`、`figure_2_dataset_features.png`、对应绘图脚本；
- `figure_3_performance_comparison_ablation.pdf`、`.png`、对应绘图脚本；
- `figure_4_uncertainty_boundaries.pdf`、`.png`、对应绘图脚本；
- `figure_plan.md`：四张图的输入、主张、子图、样式、状态和缺口；
- `figure_captions.md`：按计划图数分章节保存所有图的正式图注；

图1若 image 接口不可用，必须保存 `figure_1_prompt.md` 和 `PENDING` 说明；不得用空白图片冒充完成。缺少某张图所需证据时，保留其计划记录和阻塞原因，不用零值或模拟结果填充。

图1 的 image 接口通过主 agent 可见的 `generate_image` MCP tool 调用。生成 prompt 必须依据实际模型代码和论文计划，逐项指定准确模块名、输入/输出、箭头方向和两层 `(a) Motivation`、`(b) Architecture` 布局；不要让图像模型生成数值、引用或结论。MCP 服务使用 `PAPER_AGENT_IMAGE_BASE_URL`、`PAPER_AGENT_IMAGE_MODEL` 和 `PAPER_AGENT_IMAGE_API_KEY` 配置，密钥不得写入项目产物。tool 返回结构化状态、模型、输出路径和错误；只有确认 PNG 写入后才算成功。

### Workflow image ownership

Only the main agent calls `generate_image`; the runner never generates or overwrites images.
When the workflow requires Figure 1, call the tool during this phase at the requested
output path, then update the LaTeX includes and captions. Existing files do not replace
a successful tool receipt. Do not edit a generated PNG after the call: the runner checks
its SHA-256 against the receipt. Numerical plots must still come from measured results.
This receipt verifies generation provenance, not visual or scientific correctness.
