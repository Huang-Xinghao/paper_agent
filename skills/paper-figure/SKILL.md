---
name: paper-figure
description: 承接实验计划和论文计划，基于真实电池实验结果生成出版质量、可复现的图表、正式图注和 LaTeX 引用片段。
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, generate_image, view_image, search_wiki, read_wiki
---

# 论文图表绘制

按当前项目的实验计划和论文计划完成图表：读取真实数据、编写绘图脚本、渲染、自检并交付写作接口。
图的数量、分组、图号和子图由项目计划决定，不预设四张主图，不把总体性能、对比、消融或不确定性强行合并。
本阶段可以从已有产物计算绘图所需的派生统计，但不重新设计实验、不启动训练、不补造数据或数字。

## 工具如何配合

- 数值结果图：用 `write/edit` 编写或修改 Python 绘图脚本，用 `bash` 运行，导出 PDF 与同版 PNG；数据从真实产物读取。
- 场景/架构图：按 Runner 要求调用 `generate_image`，依据真实方法生成；不能用它生成实验曲线、指标或统计图。
- 视觉自检：调用 `view_image(path="figures/figure_3_example.png", detail="high", max_side=2048)`，当前模型会收到图片视觉输入。该工具只读图，不生成或修改图。
- 修图：数值图修改脚本后重跑；image 工具生成的 PNG 如需修改，重新调用 `generate_image`，不要直接改 PNG，以保留回执哈希一致性。

## 输入与职责边界

开始前读取：

1. `refine-logs/EXPERIMENT_PLAN.md`：各实验块的必做证据、图表目标、子图划分和评价协议。
2. `PAPER_PLAN.md`：论文主张和叙事顺序。
3. `refine-logs/FINAL_PROPOSAL.md`、实际模型代码/配置：研究任务、输入边界和已实现机制。
4. `experiment-runs/` 执行收据、`experiments/README.md` 中的计划覆盖记录（若有）、`results/` 中的真实 JSON/CSV、逐样本预测和必要的数据缓存。
5. `电池资源库/共享指南/电池论文图表通用设计指南.md`：使用其中适用的电池表达、颜色、字体和视觉检查规范。通过 `find/read` 或 `skill_resource_read` 读取本地指南，不以 Wiki 搜索无结果判定文件不存在；确实无法读取时在 `figure_plan.md` 中记录。

**计划决定内容，结果决定可画范围，指南提供视觉设计。** 共享指南中“四张主图”、固定职责/文件名、至少四个子图等模板仅作示例，不能覆盖项目已有划分或成为删并面板的依据。

`PAPER_PLAN.md` 用于组织论文，`EXPERIMENT_PLAN.md` 用于核对证据覆盖。论文计划若已删除或替换实验计划的必做证据，应记录差异，不能把它自动视为实验要求取消。只调整排版时可以改变行列、尺寸和图型，但每个原定必做面板仍须对应已绘制项、明确有依据的替代项或待补项，不能只对照缩减后的论文计划宣称覆盖完整。

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

以下代码只演示绘图写法，不代表本项目的图号、单面板布局或实验数据。实际脚本须读取真实 CSV/JSON，按第1步的面板映射排版；禁止复制示例数值作为结果。

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


### 第 5 步：渲染后自检

重新打开每张实际渲染的 PDF/PNG，按最终论文尺寸检查，而非只看脚本：

使用 `view_image` 实际查看每张最终 PNG（PDF 使用同版 PNG 预览）。它会把图片作为视觉输入送给当前模型；读取脚本、检查像素尺寸或文件存在不等于看过图片。小字不清时提高 `max_side`，修订后再次查看最新输出。在 `figure_plan.md` 记录检查的文件、SHA-256、发现的问题与修正情况；视觉输入失败时明确记录未完成视觉检查，不宣称通过。

- 标签无裁切、图例不遮数据、字号和线条可读，颜色在灰度下仍有区分。
- 面板标签、图数、数据范围和图注对应；数值、参考线、单位及指标方向与源数据一致。
- 缺失组不会显示为零，示意与实测、历史与未来、预测与真值有清晰区别。
- 负结果和失败边界未被坐标、筛选或图注掩盖；区间没有暗示不存在的重复实验。

按“渲染 → `view_image` → 修正 → 重渲染 → 再次 `view_image`”执行，直到最新版本没有已知的可修复布局问题。先看全图，小字仍不清时使用 `max_side=4096` 或生成局部预览再调用工具；不能仅凭放大的局部判断整图排版合格。复杂表格需检查其编译后的 PNG 预览，不能将 `.tex` 文本当作视觉检查。

在现有 `figure_plan.md` 中用简洁记录保存图号、查看工具返回的 SHA-256、问题、修正和最终状态；不另建评审系统。仍有数据缺口的图可标记“视觉检查通过、证据部分完成”，两者分别表述。模型/API 不接受图片或工具调用失败时记录具体错误，保留未检查状态。

**由本阶段 agent 自己完成检查与修复，不交给外部监督者补做。** 对文字重叠、箭头穿字、裁切等可修复问题，修改源脚本或重新生成图片，重新渲染并查看最终版本后才能完成。不要只报告问题就结束，也不要将未解决问题标为通过。若工具或模型持续失败，保留具体错误并报告阻塞，不能编造查看回执。

Runner 会核验 `latex_includes.tex`（含其引用的 TeX 文件）中每个 `includegraphics` 的检查记录。完成前写入 `figures/visual_checks.json`：

```json
{
  "figures": [
    {
      "asset": "figures/example.pdf",
      "preview": "figures/example.png",
      "asset_sha256": "最终交付文件的 SHA-256",
      "preview_sha256": "view_image 返回的最终预览 SHA-256",
      "status": "passed",
      "findings": "具体说明面板、文字、图例、边界和连线的检查结果，以及发现的问题",
      "corrections": "修改了什么；无需修改时说明未发现可修复的排版问题"
    }
  ]
}
```

每个实际引用的图一条，包含附录图。PNG/JPEG 直接交付时 asset 与 preview 可以相同；PDF 应渲染最终交付 PDF 生成预览，避免检查到不同版本。使用项目相对路径。每次续跑仍需用本次 `view_image` 查看复用的最终预览。文件修改后更新记录并重新查看；缺少记录、没有本次查看回执、哈希不匹配或状态未通过，Runner 会将错误反馈给你继续修复。检查记录只证明查看过对应版本，不能代替模型对实际视觉问题的判断。

### 第 6 步：同步交付和续跑

- 图注逐面板对应最新成图，注明电芯/循环/窗口粒度、split、单位、样本数及区间定义；不要把训练或诊断指标写成正式测试结果。
- `figure_captions.md` 与 `latex_includes.tex` 使用一致的图号、路径和正式 caption；未生成项保留状态说明，不使用空白图或不存在的图片路径冒充完成。
- 续跑时先检查已有图和脚本。输入、计划与输出仍适用的图可以复用，但仍需通过 `view_image` 查看；仅重画有缺口或问题的图，不默认重跑所有绘图脚本。
- Runner 要求本次生成图1时，仍需本次成功的 `generate_image` 回执；已有图和 `view_image` 回执均不能替代生成回执。接口失败时明确报告图1未完成。

## 必须产出

在项目 `figures/` 下维护：

- `figure_plan.md`：上游图表/子图映射、输入、字段、判断任务、输出和完成状态；缺口保留到具体证据项。
- `paper_plot_style.py` 及每张数据图的可重跑脚本。
- 按计划实际生成的 PDF、至少 300 dpi PNG，以及独立 LaTeX 表格；不固定总数或图2之后的文件名。
- 架构图的可编辑源/生成记录；Runner 要求的图1保留约定 PNG 路径和回执。
- `figure_captions.md`、`latex_includes.tex`：与实际产物和计划 ID 对应，供后续写作读取。
- `visual_checks.json`：最终交付图及预览的哈希、具体检查结果和修复记录，供 Runner 核验。
