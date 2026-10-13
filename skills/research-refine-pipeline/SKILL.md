---
name: research-refine-pipeline
description: 兼容入口：研究方案和实验计划已拆分为 research-refine 与 experiment-plan 两个架构阶段。
allowed-tools: Read, Write, Edit, Grep, Glob
---

# 研究细化兼容入口

当前 workflow 不再把方案细化和实验规划封装成一个 skill。正式节点顺序是：

`research-refine -> experiment-plan -> experiment-bridge`

- `research-refine` 读取已批准的 idea、review 和 novelty 产物，写入
  `refine-logs/FINAL_PROPOSAL.md`。
- `experiment-plan` 读取最终方案，写入
  `refine-logs/EXPERIMENT_PLAN.md` 和 `refine-logs/EXPERIMENT_TRACKER.md`。
- `experiment-bridge` 根据 tracker 和最终方案生成可执行实验 manifest；随后由 runner 执行 smoke 和正式实验，完成后进入 `paper-plan`。

本兼容入口不启动子流程，不调用 ARIS slash command、外部 MCP 或旧的 pipeline 命令，也不重复搜索
架构已经提供的文献和 Wiki。电池资源库只在需要核对材料体系、工况、数据字段或方法细节时按路径读取；
不能把资源库说明当作实验结果。旧配置进入此 skill 时，应按上述正式节点顺序恢复 workflow。
