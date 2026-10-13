---
name: paper-figure
description: "根据实验结果生成出版质量的图表。当用户说“画图”、“作图”、“生成图”、“论文图”或需要论文图时使用。"
argument-hint: "[figure-plan-or-data-path]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, mcp__codex__codex, mcp__codex__codex-reply
---

# 论文图：实验数据的出版质量图

生成论文的所有图表基于：**$ARGUMENTS**

## 范围：该技能可以做什么和不能做什么

| 类别 | 可以自动生成吗？ | 示例 |
|----------|-------------------|----------|
| **数据驱动图** | ✅ 是的 | 线图（训练曲线）、条形图（方法比较）、散点图、热图、箱线图／小提琴图 |
| **比较表** | ✅ 是的 | LaTeX 表格比较先验界限、方法特征、消融实验结果 |
| **多面板图** | ✅ 是的 | 组合多个图的子图网格（例如，3×3数据集×方法） |
| **架构／流程图** | ❌ 否 — 手动 | 模型架构、数据流程图、系统概述。最多可以生成一个粗略的 TikZ 骨架，但**希望自己使用draw.io、Figma 或 TikZ 等工具绘制这些** |
| **生成的图像网格** | ❌ 否 — 手动 | 生成样本的网格（例如，GAN/diffusion 输出）。这些来自运行模型，而不是来自此技能 |
| **照片/截图** | ❌ 否 — 手动 | 真实世界图像、UI 屏幕截图、定性示例 |

**实践中：** 对于典型的 ML 论文，该技能可处理约 60% 的图形（所有数据图 + 表格）。剩余的~40%（概览主图、架构图、定性结果）需要在运行 `/paper-write` 之前手动创建并放置在 `figures/` 中。该技能会将它们检测为“现有图形”并保存它们。

## 常量

- **STYLE = `publication`** — 视觉样式预设。选项：`publication`（默认，干净打印）、`poster`（较大字体）、`slide`（粗体颜色）
- **DPI = 300** — 输出分辨率
- **FORMAT = `pdf`** — 输出格式。选项：`pdf`（矢量，最适合 LaTeX）、`png`（光栅后备）
- **COLOR_PALETTE = `tab10`** — 默认 matplotlib 颜色循环。选项：`tab10`、`Set2`、`colorblind`（绿色色盲友好）
- **FONT_SIZE = 10** — 基本字体大小（与典型的会议正文文本匹配）
- **FIG_DIR = `figures/`** — 生成的图形的输出目录
- **REVIEWER_MODEL = `gpt-6-astra`** — 通过 Codex MCP 用于图形质量审稿的模型。

## 输入

1. **PAPER_PLAN.md** — 图形计划表（来自 `/paper-plan` ）
2. **实验数据** — JSON 文件、CSV 文件或 `figures/` 或项目根目录中的 screen 日志
3. **现有图形** — 任何要保留的手动创建的图形

如果不存在 PAPER_PLAN.md，则扫描数据文件并询问用户要生成哪些图表。

## 工作流程

### 第 1 步：阅读图表规划

解析 PAPER_PLAN.md 中的图形计划表：

```markdown
| ID | 类型 | 描述 | 数据来源 | 优先事项 |
|----|------|-------------|-------------|----------|
| 图1 | 架构图 | ... | 手动的 | 高的 |
| 图2 | 线图 | ... | figures/exp.json | 高的 |
```

确认：
- 哪些图表可以根据数据自动生成
- 需要手动创建（架构图等）
- 哪些是比较表（生成为 LaTeX ）

### 第2步：设置绘图环境

创建共享样式配置脚本：

```python
# paper_plot_style.py — 在所有图形脚本之间共享
import matplotlib.pyplot as plt
import matplotlib
matplotlib.rcParams.update({
    'font.size': FONT_SIZE,
    'font.family': 'serif',
    'font.serif': ['Times New Roman', 'Times', 'DejaVu Serif'],
    'axes.labelsize': FONT_SIZE,
    'axes.titlesize': FONT_SIZE + 1,
    'xtick.labelsize': FONT_SIZE - 1,
    'ytick.labelsize': FONT_SIZE - 1,
    'legend.fontsize': FONT_SIZE - 1,
    'figure.dpi': DPI,
    'savefig.dpi': DPI,
    'savefig.bbox': 'tight',
    'savefig.pad_inches': 0.05,
    'axes.grid': False,
    'axes.spines.top': False,
    'axes.spines.right': False,
    'text.usetex': False,  # 如果 LaTeX 可用，则设置为 True
    'mathtext.fontset': 'stix',
})

# 调色板
COLORS = plt.cm.tab10.colors  # 或 Set2，或色盲安全

def save_fig(fig, name, fmt=FORMAT):
    """Save figure to FIG_DIR with consistent naming."""
    fig.savefig(f'{FIG_DIR}/{name}.{fmt}')
    print(f'Saved: {FIG_DIR}/{name}.{fmt}')
```

### 第 3 步：自动选择图形类型

使用此决策树来处理数据驱动的图形（受到 Imbad0202/academic-research-skills 的启发）：

| 数据模式 | 推荐类型 | 尺寸 |
|-------------|-----------------|------|
| X= time/steps , Y=指标 | 线图 | 0.48\textwidth |
| 方法 × 1 指标 | 条形图 | 0.48\textwidth |
| 方法 × 多个指标 | 分组条形/雷达 | 0.95\textwidth |
| 两个连续变量 | 散点图 | 0.48\textwidth |
| 矩阵/网格值 | 热图 | 0.48\textwidth |
| 分布比较 | 箱线图／小提琴图 | 0.48\textwidth |
| 多数据集结果 | 多面板（子图） | 0.95\textwidth |
| 之前的工作比较 | LaTeX表 | — |

### 第 4 步：生成每个图形

对于计划中的每个图形，创建一个独立的 Python 脚本：

**线图**（训练曲线、缩放）：
```python
# gen_fig2_training_curves.py
from paper_plot_style import *
import json

with open('figures/exp_results.json') as f:
    data = json.load(f)

fig, ax = plt.subplots(1, 1, figsize=(5, 3.5))
ax.plot(data['steps'], data['fac_loss'], label='Factorized', color=COLORS[0])
ax.plot(data['steps'], data['crf_loss'], label='CRF-LR', color=COLORS[1])
ax.set_xlabel('Training Steps')
ax.set_ylabel('Cross-Entropy Loss')
ax.legend(frameon=False)
save_fig(fig, 'fig2_training_curves')
```

**条形图**（比较，消融实验）：
```python
fig, ax = plt.subplots(1, 1, figsize=(5, 3))
methods = ['Baseline', 'Method A', 'Method B', 'Ours']
values = [82.3, 85.1, 86.7, 89.2]
bars = ax.bar(methods, values, color=[COLORS[i] for i in range(len(methods))])
ax.set_ylabel('Accuracy (%)')
# 在条上添加值标签
for bar, val in zip(bars, values):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
            f'{val:.1f}', ha='center', va='bottom', fontsize=FONT_SIZE-1)
save_fig(fig, 'fig3_comparison')
```

**比较表**（LaTeX，理论论文）：
```latex
\begin{table}[t]
\centering
\caption{Comparison of estimation error bounds. $n$: sample size, $D$: ambient dim, $d$: latent dim, $K$: subspaces, $n_k$: modes.}
\label{tab:bounds}
\begin{tabular}{lccc}
\toprule
Method & Rate & Depends on $D$? & Multi-modal? \\
\midrule
\citet{MinimaxOkoAS23} & $n^{-s'/D}$ & Yes (curse) & No \\
\citet{ScoreMatchingdistributionrecovery} & $n^{-2/d}$ & No & No \\
\textbf{Ours} & $\sqrt{\sum n_k d_k / n}$ & No & Yes \\
\bottomrule
\end{tabular}
\end{table}
```

**Architecture/pipeline 图**（手动 - 超出本技能的范围）：
- 这些需要使用draw.io、Figma、Keynote 或TikZ 手动创建
- 该技能可以生成一个粗略的 TikZ 骨架作为起点，但**不要期望达到出版质量的结果**
- 如果该图已存在于 `figures/` 中，则保留它并仅生成 LaTeX `\includegraphics` 代码段
- 图中标记为`[MANUAL]`，`latex_includes.tex`

### 第 5 步：运行所有脚本

```bash
# 运行所有图形生成脚本
for script in gen_fig*.py; do
    python "$script"
done
```

验证所有输出文件是否存在且非空。然后**渲染然后验证**：重新打开每个渲染的 PDF/PNG（不是脚本）并自检 - 没有剪切标签，没有图例覆盖数据，每个 number/label 在最终打印尺寸下可读。这种自查发生在第7步审稿之前，因此审稿人的预算花在实质内容上，而不是用来抓斧头。

### 第 6 步：生成 LaTeX 包含片段

对于每个图，输出 LaTeX 代码以包含它：

```latex
% === 图 2：训练曲线 ===
\begin{figure}[t]
    \centering
    \includegraphics[width=0.48\textwidth]{figures/fig2_training_curves.pdf}
    \caption{Training curves comparing factorized and CRF-LR denoising.}
    \label{fig:training_curves}
\end{figure}
```

将所有片段保存到 `figures/latex_includes.tex`，以便轻松复制粘贴到论文中。

### 第7步：用REVIEWER_MODEL进行图质审稿

将图说明和说明文字发送至 GPT-6-Astra 审稿：

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    这些figure/table审稿计划提交[目标会议或期刊]。

    对于每个数字：
    1. 标题是否内容丰富且独立？
    2. 图形类型与显示的数据匹配吗？
    3. 比较是否公平、清晰？
    4. 是否有缺失基线或消融实验？
    5. 不同的可视化效果会更有效吗？

    [列出所有带有标题和描述的图]
```

### 第 8 步：质量检查表

检查表是分区的（来自 Anthropic 的 Claude 科学 `figure-style` 技能，Apache-2.0 的模式）： **正确性规则始终具有约束力** - 它们是关于图形是否说真话，没有审美内容，并且没有任何风格选择可以覆盖它们； **指导规则是默认的** - 它们会产生干净的结果，但经过深思熟虑的、规定的替代方案可能会覆盖它们。

**正确性 - 始终绑定，在渲染之前验证数据：**

- [ ] **排除的数据永远不会进入摘要** - 源中的一行 excluded/flagged 要么完全消失，要么明显不同（开放/阴影线标记，在键中命名）；它从不提供与包含的行一起绘制的 mean/CI
- [ ] **标题和任何类似论断的标题文本都会针对每个绘制的行进行测试** - 如果一个类别与论断相矛盾，则对其进行限定（“在 4 个基准中的 3 个基准上”）或降级为描述；即使渲染得很漂亮，夸大其辞的数字也是错误的
- [ ] **仅限可比条件** - 在不同 N / 预算 / 协议下测量的武器不绘制为视觉对等；将它们分开或在标题中标记差异
- [ ] **说明 n 以及固定的内容** — 每个带有摘要标记的面板均表示 n 和复制单位（面板或标题）
- [ ] **渲染然后验证** — 对渲染的 PDF/PNG（不是脚本）的第 5 步自检实际上发生了：没有剪切标签，没有覆盖数据的图例，每个 number/label 在最终打印尺寸下都可读

**指导 - 强默认值（来自 pedrohcgs/claude-code-my-workflow ），故意规定的替代方案可能会覆盖 - 除了下面的关键规则变得困难的项目（矢量 PDF 输出和无标题内数字是关键规则：将这两个视为具有约束力，不可覆盖）：**

- [ ] 字体大小与打印纸张大小相同（不要太小）
- [ ] 灰度中可区分的颜色（适合打印）
- [ ] **图中没有标题** — 标题仅出现在 LaTeX `\caption{}` 中（来自 pedrohcgs）
- [ ] 图例不与数据重叠
- [ ] 轴标签在适用的情况下具有单位
- [ ] 轴标签具有出版物质量（不是像 `emp_rate` 这样的变量名称）
- [ ] 图形宽度适合单列 (0.48\textwidth) 或全宽 (0.95\textwidth)
- [ ] PDF 输出是矢量（不是光栅化文本）
- [ ] 没有 matplotlib 默认标题（为出版物删除 `plt.title`）
- [ ] Serif 字体与论文正文匹配 (Times / Computer Modern)
- [ ] 色盲可访问（如果使用色盲调色板）

## 输出

```
figures/
├── paper_plot_style.py # 共享样式配置
├── gen_fig1_architecture.py # 每个图形的脚本
├──  gen_fig2_training_curves.py
├──  gen_fig3_comparison.py
├── fig1_architecture.pdf # 生成的人物
├──  fig2_training_curves.pdf
├──  fig3_comparison.pdf
├── latex_includes.tex # LaTeX 所有人物的片段
└── TABLE_*.tex # 独立表LaTeX文件
```

## 主要规则

- **每个图形都必须是可重复的** - 将生成脚本与输出一起保存
- **不要对数据进行硬编码** — 始终从 JSON/CSV 文件中读取
- **对所有绘图使用矢量格式 ( PDF )** — PNG 仅作为后备
- **没有装饰元素** — 没有背景颜色、没有 3D 效果、没有图表垃圾
- **所有图形的风格一致** - 相同的字体、颜色、线宽
- **色盲安全** — 如果需要，请与 https://davidmathlogic.com/colorblind/ 进行验证
- **每个图形一个脚本** — 当数据发生变化时，可以轻松地重新运行各个图形
- **图中没有标题** - 标题仅在 LaTeX 中
- **比较表算作数字** - 将它们生成为独立的 .tex 文件

## 图类型参考

| 类型 | 何时使用 | 典型尺寸 |
|------|------------|--------------|
| 线图 | 训练曲线、缩放趋势 | 0.48\textwidth |
| 条形图 | 方法比较、消融实验 | 0.48\textwidth |
| 分组栏 | 多指标比较 | 0.95\textwidth |
| 散点图 | 相关性分析 | 0.48\textwidth |
| 热图 | 注意力、混淆矩阵 | 0.48\textwidth |
| Box/violin | 分布比较 | 0.48\textwidth |
| 建筑学 | 系统概述 | 0.95\textwidth |
| 多面板 | 综合结果（子图） | 0.95\textwidth |
| 对照表 | 先验界限与我们的界限（理论） | 全宽 |

## 致谢

设计模式（类型×风格矩阵）的灵感来自于[baoyu-skills](https://github.com/jimliu/baoyu-skills)。发布样式默认值和图形规则来自 [ pedrohcgs/claude-code-my-workflow ](https://github.com/pedrohcgs/claude-code-my-workflow)。来自[Imbad0202/academic-research-skills](https://github.com/Imbad0202/academic-research-skills)的可视化决策树。

