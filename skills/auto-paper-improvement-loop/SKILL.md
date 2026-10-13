---
name: auto-paper-improvement-loop
description: 根据独立论文审阅逐轮修正论文、保持证据边界并重新编译，供工作流自动改稿节点使用。
---

# 自动论文改进循环

此 skill 在 `paper-plan -> paper-figure -> paper-write -> paper-compile` 之后运行。runner 负责启动独立审阅、控制轮数、保存 PDF 快照和调用编译；本 skill 在单轮中读取当前论文和审阅意见，实施有证据支持的最小修改，然后结束，不自行启动下一轮审阅。

## 每轮输入

- 当前 `paper/main.pdf` 和所有 `paper/sections/*.tex`
- `PAPER_PLAN.md`、`refine-logs/FINAL_PROPOSAL.md`
- `results/`、图表和 `paper/references.bib`
- runner 注入的独立审阅意见

## 修改原则

先把意见分类为证据/正确性、结构/逻辑、表达、图表、引用和排版。优先修复会改变审稿人判断的高优先级问题。每项修改都要回到实际结果、原始文献或方案核对；不能凭空添加数字、基线、数据集、引用、定理或实验结论。负结果和证据不足应通过降级措辞、显式局限或 `PENDING` 说明处理，不得把缺口写成已完成。

保持最终方案的问题锚点、方法、数据对象、实体级 split、标签和指标不变。若意见要求补实验或改变研究设计，记录为待办交给 `experiment-plan`/`experiment-bridge`，本轮只做不会改变证据含义的文本修正。保持作者匿名和已有引用纪律。

## 独立性与审计

每轮审阅应使用新鲜的独立上下文；不要把上一轮评分、修复清单或“已经改好”的描述传给审阅者。runner 维护 `paper/PAPER_IMPROVEMENT_LOG.md` 和每轮 PDF 快照；本 skill 结束时报告改动文件、未采纳意见、剩余证据缺口和需要重新编译的内容。不得编辑结果文件、伪造审阅意见或自行宣称论文已通过审阅。
