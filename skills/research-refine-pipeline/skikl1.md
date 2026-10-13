---
name: research-refine-pipeline
description: "运行链接 `research-refine` 和 `experiment-plan` 的端到端工作流程。当用户想要从模糊的研究方向到集中的最终建议加上详细的实验路线图的一次性流程时使用，或者要求“串起来”，构建一个流程，端到端地执行，或者一起生成方法和实验计划。"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, mcp__codex__codex, mcp__codex__codex-reply
---

# 研究细化流程：端到端方法和实验规划

细化并具体化：**$ARGUMENTS**

## 概述

当用户不想停留在精炼方法上时，可以使用此技能。目标是制定一个连贯的一揽子计划，其中包括：

- 一个以问题为中心、优雅的最终提案
- 审稿历史解释了为什么该方法是集中的
- 与论文的论断相关的详细实验路线图
- 一个紧凑的流程摘要，说明下一步要运行什么

该技能由两个现有工作流程组成：

1. `research-refine` 用于方法细化
2. `experiment-plan` 以论断为导向验证规划

有关特定阶段的详细信息，请仅在需要时阅读这些同级技能：

- `../research-refine/SKILL.md`
- `../experiment-plan/SKILL.md`

## 核心规则

不要在不稳定的方法之上规划大型实验集合。首先稳定核心论点。然后将稳定的核心论点转化为实验。

## 默认输出

- `refine-logs/FINAL_PROPOSAL.md`
- `refine-logs/REVIEW_SUMMARY.md`
- `refine-logs/REFINEMENT_REPORT.md`
- `refine-logs/EXPERIMENT_PLAN.md`
- `refine-logs/EXPERIMENT_TRACKER.md`
- `refine-logs/PIPELINE_SUMMARY.md`

## 工作流程

### 第 0 阶段：对起点进行分类

- 提取问题、粗略方法、约束、资源和目标会议或期刊。
- 检查`refine-logs/FINAL_PROPOSAL.md`是否已经存在并且仍然匹配当前请求。
- 如果提案丢失、过时或与当前请求存在重大差异，则运行完整的 `research-refine` 阶段。
- 如果提案已经很强大并且一致，请重用它并跳转到实验计划。
- 如果有疑问，最好重新运行 `research-refine`，而不是为错误的方法计划实验。

### 第一阶段：方法细化阶段

运行 `research-refine` 工作流程并保持其 V3 理念完整：

- 保留问题锚点
- 更喜欢最小的适当机制
- 保留一个主要贡献
- 只有在改进论文时才进行现代化

仅当这些明确时才退出此阶段：

- 最终方法核心论点
- 主要贡献
- 故意拒绝的复杂性
- 关键论断和必须执行的消融实验
- 剩余风险（如果有）

如果结论仍然是 `REVISE` ，则仅在清楚记录剩余弱点的情况下才继续进行实验计划。

### 第二阶段：规划门

在实验阶段之前，写一个简短的门检查：

- 最终方法核心论点是什么？
- 主要贡献是什么？
- 故意拒绝什么复杂性？
- 审稿人关注哪些事项尚待审定？
- 前沿技术基元是核心、可选还是不存在？

如果这些答案不明确，请先收紧最终提案。

### 第三阶段：实验计划阶段

运行 `experiment-plan` 工作流程基于：

- `refine-logs/FINAL_PROPOSAL.md`
- `refine-logs/REVIEW_SUMMARY.md`
- `refine-logs/REFINEMENT_REPORT.md`

确保实验计划涵盖：

- 主锚结果
- 新颖性独立验证
- 简单性或删除检查
- 前沿技术必要性检查（如果适用）
- 运行顺序、预算和决策检查点

### 第四阶段：整合总结

写入 `refine-logs/PIPELINE_SUMMARY.md` ：

```markdown
# 流程总结

**问题**：[问题]
**最终方法核心论点**：【一句话】
**最终判决**：[ READY / REVISE / RETHINK ]
**日期**：[今天]

## 最终交付成果
- 建议：`refine-logs/FINAL_PROPOSAL.md`
- 审稿摘要：`refine-logs/REVIEW_SUMMARY.md`
- 实验计划：`refine-logs/EXPERIMENT_PLAN.md`
- 实验跟踪器：`refine-logs/EXPERIMENT_TRACKER.md`

## 贡献快照
- 主要贡献:
- 可选辅助贡献:
- 明确拒绝复杂性：

## 必须验证的论断
- [论断 1]
- [论断 2]

## 首次运行启动
1. [运行]
2. [运行]
3. [运行]

## 主要风险
- [风险]：
- [减轻]：

## 下一步行动
- 继续至 `/run-experiment`
```

### 第 5 阶段：向用户提供简短摘要

```
流程完成。

方法输出：
- refine-logs/FINAL_PROPOSAL.md

实验输出：
- refine-logs/EXPERIMENT_PLAN.md
- refine-logs/EXPERIMENT_TRACKER.md

流程总结：
- refine-logs/PIPELINE_SUMMARY.md

最好的下一步：
- /run-experiment
```

## 输出协议

> 所有输出文件均遵循以下共享协议：
> - **[输出版本控制协议](../shared-references/output-versioning.md)** — 首先写入带时间戳的文件，然后复制到固定名称
> - **[输出清单协议](../shared-references/output-manifest.md)** — 将每个输出记录到 MANIFEST.md
> - **[输出语言协议](../shared-references/output-language.md)** — 尊重项目的语言设置

## 主要规则

- **大文件处理**：如果写入工具因文件大小而失败，请立即使用 Bash ( `cat << 'EOF' > file` ) 重试分块写入。不要征求用户的许可——只需默默地进行即可。

- 不要让实验计划凌驾于问题锚点之上。
- 除非确实需要缺少验证块，否则不要在方法细化后扩大论文故事。
- 在 `FINAL_PROPOSAL.md`、`EXPERIMENT_PLAN.md` 和 `PIPELINE_SUMMARY.md` 上重复使用相同的论断。
- 保持论文的主要故事紧凑。
- 如果方法故意简单，请在实验计划中维护这种简单性，而不是添加新组件。
- 如果该方法使用现代 LLM / VLM / Diffusion / RL 技术基元，请明确其必要性测试。
- 如果方法不需要前沿技术基元，就说清楚，避免强行提出。
- 当用户只需要一个阶段时，优先选择阶段性技能；使用此技能进行集成流程。

## 与其他技能组合

```
/research-refine-pipeline -> 一次性方法+实验规划
/research-refine -> 仅方法细化
/experiment-plan -> 仅实验计划
/run-experiment -> 执行
```

