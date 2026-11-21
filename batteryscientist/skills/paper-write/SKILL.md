---
name: paper-write
description: 根据论文规划、真实实验结果、图表和核实文献生成中文或指定语言的 LaTeX 论文源文件。
---

# 论文写作

读取 `PAPER_PLAN.md`、`refine-logs/FINAL_PROPOSAL.md`、`refine-logs/EXPERIMENT_PLAN.md`、`results/`、`figures/latex_includes.tex` 和现有论文源文件，按计划写入 `paper/main.tex`、`paper/sections/*.tex`、`paper/references.bib`。保持章节、主张、图表和证据之间的对应关系。正文和附录均按论文语言组织，不在正文中堆砌实验日志、运行记录或工程检查。切勿写成**实验报告**
### 第 1 步：初始化项目

1. 创建`paper/`目录
2. 未指定会议或期刊时，默认使用 `templates/nature.tex` 作为 `paper/main.tex`，将 `templates/nature-citations.sty` 和 `templates/sn-nature.bst` 一起复制到 `paper/`。这是通用论文排版配 Nature 引用样式；指定投稿格式时改用相应模板。已有论文保留正文排版，未指定引用样式时接入相同的 Nature 引用配置。
3. 使用论文特定符号生成 `math_commands.tex`
4. 创建匹配 PAPER_PLAN 结构的节文件

默认通用稿使用 `\pagestyle{plain}`：无页眉，仅页脚居中页码；在 `\begin{document}` 后设置，附录沿用。`ctexart`中文方案默认启用章节页眉，未覆盖时会在图表页重复显示“D 训练、成本与可复现性”等附录标题。这里的D是章节号，不是图号。已有通用稿也应用此约定；明确指定期刊时遵循期刊页眉规范。编译后检查正文、附录和整页图表页，确认没有残留章节页眉。

**作者封锁（匿名模式）：**
```latex
\author{Anonymous Authors}
```

### 步骤2：生成math_commands.tex

根据论文的符号创建共享数学宏：

```latex
% math_commands.tex — 共享符号
\newcommand{\R}{\mathbb{R}}
\newcommand{\E}{\mathbb{E}}
\DeclareMathOperator*{\argmin}{arg\,min}
\DeclareMathOperator*{\argmax}{arg\,max}
% 在此处添加论文特定的符号
```

### 第 3 步：写下每个部分

按顺序处理部分。对于每个部分：

1. **阅读计划** — 读取本节的段落任务、主张、研究证据和引用计划，复用已核实来源及已有引用键。
2. **内容草稿** — 根据已有证据编写完整的 LaTeX，不编造缺失的数据或结果。
3. **插入 figures/tables** — 使用 `figures/latex_includes.tex` 中的片段及正式 caption；图注、表注保持简洁，修改统一更新该文件。
4. **同步添加引文** — 起草相关句子时使用引用计划中支持该论点的文献，并将实际引用的可靠元数据写入 `paper/references.bib`；默认 Nature 样式使用 `\cite{key}` 生成数字上标，按首次引用顺序编号；叙述作者时写作 `Severson等\cite{key}`，不用 `\citet` 自动生成作者名。指定 ML 模板时使用其支持的 `\citep` / `\citet`，IEEE 模板使用 `cite` 包和 `\cite`。

在起草前面的内容之前，请重新阅读 `PAPER_PLAN.md` 的一句话贡献。在读者了解完整方法之前，摘要和引言应该使这一点显而易见。

各部分起草前，先读取并遵循电池资源库的[论文写作指南](../../../电池资源库/代码和绘图指南/论文写作.md)：

```text
battery_resources(action="read", resource_id="代码和绘图指南/论文写作")
```

摘要、引言、数据集、实验、方法和总结与讨论的写作逻辑以该指南为准。

### 第 4 步：引用补漏与参考书目整理

1. 对照 `PAPER_PLAN.md` 和正文补齐文献依据，复用已核实来源。缺口按关键词用 `search_wiki` 检索、`read_wiki` 阅读；Wiki 不足时用 `search_papers` 补充。
2. 新增文献用 `search_papers` 核对书目信息，优先采用正式发表版本，写入 `paper/references.bib`，并将支撑论点和来源链接同步到引用计划。
3. 整理正文引用与 `.bib`，合并重复论文，保留实际引用条目。正文至少引用 20 篇不同论文，不足时围绕相关论述补充，交付时报告实际篇数。
   统计从主入口实际加载的正文引用，排除注释、未加载文件、仅附录引用和 `\nocite`；同一论文的多个引用键只计一篇。书目条目数不等于正文引用篇数。已有计划不足时继续定向核实并同步补齐计划；负结果、材料足够或不凑数量均不能豁免这一要求。不足20篇时保存草稿并报告未完成，不能仅记录限制后宣称完成。
4. 默认使用 Nature 引用配置：`\usepackage{paper/nature-citations}` 和 `\bibliographystyle{paper/sn-nature}`，从项目根目录编译。`.bib` 保留完整元数据，作者缩写、标点和链接展示交给样式文件；指定期刊时遵循对应模板。
