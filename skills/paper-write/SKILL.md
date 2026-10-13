---
name: paper-write
description: 根据论文规划、真实实验结果、图表和核实文献生成中文或指定语言的 LaTeX 论文源文件。
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, search_papers, verify_papers, search_wiki, read_wiki
---

# 论文写作

读取 `PAPER_PLAN.md`、`refine-logs/FINAL_PROPOSAL.md`、`refine-logs/EXPERIMENT_PLAN.md`、`results/`、`figures/figure_captions.md`、`figures/latex_includes.tex` 和现有论文源文件，按计划写入 `paper/main.tex`、`paper/sections/*.tex`、`paper/references.bib`。保持章节、主张、图表和证据之间的对应关系。

## 写作原则

论文只陈述已被结果或核实文献支持的内容。计划中的实验、缺失的数字、未验证的引用和未完成的图表必须标记为证据缺口，不得写成结果。不要引入最终方案和实验计划之外的新方法、新数据集或新主张；保留已有论文中仍然有效的内容，并避免无必要覆盖用户文件。

围绕一个主贡献组织叙事：引言说明问题、缺口、方法和证据；相关工作按方法家族/假设综合比较；方法定义电池对象、标签、观测窗口、实体级 split、训练与推理；实验说明数据、基线、公平设置、主结果、消融、稳健性和失败案例；讨论写清适用边界和局限。结论不添加新主张。

电池事实必须说明材料/体系、实验或车辆/储能/退役场景、任务（RUL/SOH/SOC/轨迹等）、标签构造、EOL/删失、预处理和指标。不得把电池资源库描述或 Wiki 摘要当作测量结果。

## 引用与图表

只使用已有 `.bib` 或通过论文工具核实的条目，核对作者、年份、venue 和 DOI/来源；不凭记忆生成 BibTeX，不确定时标记 `[VERIFY]` 并列入缺口。引用 `figures/latex_includes.tex` 中的真实图，检查图注是否说明比较对象、数据源、单位、不确定性和支持的主张。作者信息使用匿名或项目约定的占位符，不自行生成个人信息。

## 图注解驱动的正文叙事

写作前必须读取 `figures/figure_captions.md` 和 `figures/latex_includes.tex`。`figure_captions.md` 按 `Figure 1`、`Figure 2` 等章节保存每张图的正式图注；正文引用图表时使用对应章节中的图注信息，并确保数字、样本量、指标、单位和区间定义与真实结果一致。

默认的图表故事顺序为：

1. 图1：交代研究场景、核心困难、方法动机和模型/系统架构；
2. 图2：交代数据集、输入特征、标签/任务定义和实验协议；
3. 图3：呈现总体性能、公平基线比较、跨数据集结果和消融归因；
4. 图4：检验不确定性、稳健性、失败边界和适用范围。

首次讨论某一张图时引用对应图号，并按图注中的子图顺序解释内容。图注中的 `PENDING`、计划项或证据缺口不得改写成已验证结果。`figures/latex_includes.tex` 中的 caption 应与 `figure_captions.md` 对应章节一致；不一致时记录版本问题，不得静默编造或扩展图注。

## 必须检查和产出

确保 `paper/sections/` 的文件与 `PAPER_PLAN.md` 章节数量一致，`main.tex` 能包含所有章节，引用键存在，图路径有效，且没有未解释的 `PENDING` 数值性表述。写入 `paper/main.tex`、所有章节文件和 `paper/references.bib`；不要自行编译，交给 `paper-compile` 节点。完成时汇报证据缺口和待核实引用。
