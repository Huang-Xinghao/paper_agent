---
name: research-review
description: 对 Idea Report 做严格、可执行的研究评审，输出确定的 0--10 分、结论和主 agent 可直接采用的修改意见。
---

# 电池研究 Idea 评审

## 任务边界

本 skill 评审 `idea-stage/IDEA_REPORT.md`、`idea-stage/NOVELTY.json` 及其证据，判断当前 Idea 是否清楚、可证伪、可实施，以及新颖性主张是否被最接近工作支撑，并把问题转化为下一轮主 agent 可以执行的修改任务。它不替主 agent 修改 Idea Report。主 agent 在收到评审后负责修订；workflow 负责保存结构化 `REVIEW.json` 和可读的 `REVIEW.md`，然后根据结论重跑 ideas 或进入 refine。

评审本身必须成为一份有批判性、建设性和可执行性的意见：指出关键缺陷，引用证据或缺失证据，给出必须修改的内容。

这是 idea 评审，不是数据审计或实验验收。idea 阶段不要求完整读取数据、实际构造标签、
冻结数据划分或得到模型结果；这些尚未完成本身不得作为 `BLOCKER`、强制修订条件或
压低可行性评分的唯一理由。检查报告是否给出合理的数据核查计划、失败判据和收缩路径；
未核验的数据事实可记为后续实验前检查事项。只有 idea 本身依赖已知不可能的输入、
自相矛盾的数据契约或缺少任何可执行验证路径时，才按相应严重程度指出方法问题。

## 评审标准

1. **解决问题的重要性**：问题是否对应真实且重要的科学或工程痛点，目标场景是否明确，解决后是否会改变预测、决策或实验实践；检查是否把多个弱问题拼接成一个目标；
2. **使用方法的新颖性**：方法是否超出换骨干、堆模块、增加概率头或改变指标；最接近工作是否覆盖同一问题契约、输入、输出和评价；
3. **所提出方法的合理性**：方法是否从问题机制推出，输入在决策时点是否可获得，假设是否可检验，方法是否有能力解决所定义的问题；
4. **证据与新颖性核验**：`NOVELTY.json` 是否找到真正最接近的工作，venue、方法和差异是否有来源；


## 确定评分与结论

必须给出一个 `0--10` 的数值评分，不能只给区间或“看起来不错”：

- `0--3`：核心问题、证据或可行性存在致命缺陷；
- `4--6`：有价值的方向，但需要实质性修订后才能进入下一阶段；
- `7--8`：主张、证据和最小验证基本成立，可进入 novelty/refine；
- `9--10`：边界、证据和验证契约都非常清楚，只有小的表达或执行风险。

结论只能是 `PROCEED`、`PROCEED_WITH_CAUTION`、`REVISE` 或 `REJECT`。评分与结论必须一致：`PROCEED` 通常至少 7 分；`REVISE` 说明下一轮需要修订；`REJECT` 必须给出导致放弃的具体证据。

除了总分，必须分别给出以下三个独立的 `0--10` 评分，并解释每个评分的证据：

- `problem_importance`：解决问题的重要性；
- `method_novelty`：使用方法的新颖性；
- `method_soundness`：所提出方法的合理性。

新颖性证据不足时必须降低 `method_novelty` 或明确标记为未定，不能因为差异尚未核实就给高分；方法尚未证明能解决问题时，必须在 `method_soundness` 中反映。总分应综合这三个维度、证据质量和实验可执行性。

## 强制响应格式

最终响应必须是可解析的 JSON，不要在 JSON 外添加 Markdown 或解释文字：

```json
{
  "score": 0,
  "verdict": "REVISE",
  "confidence": 0.0,
  "reviewer_model": "当前实际模型标识或 unknown",
  "dimension_scores": {
    "problem_importance": {"score": 0, "assessment": "重要性判断及证据"},
    "method_novelty": {"score": 0, "assessment": "新颖性判断及证据"},
    "method_soundness": {"score": 0, "assessment": "合理性判断及证据"}
  },
  "summary": "一句话总评",
  "critical_issues": [
    {
      "severity": "BLOCKER|MAJOR|MINOR",
      "issue": "具体问题",
      "evidence": "对应字段、来源或缺失证据",
      "required_change": "主 agent 下一轮必须做的修改",
      "resolution_test": "可判定问题是否解决的最小检查或实验"
    }
  ],
  "required_changes": ["必须修改的内容，按优先级排列"],
  "minimum_next_steps": ["按优先级排列的最小修改步骤"],
  "novelty_assessment": "与 novelty-check 结论的关系及未决风险",
  "battery_checks": ["idea 层面的信息契约和潜在泄漏风险"],
  "evidence_gaps": ["尚未核验的论文事实和后续实验前待检查的数据条件"]
}
```

`score` 必须是 0 到 10 的数字，`confidence` 必须是 0 到 1 的数字；不得虚构 reviewer、实验结果、样本量、指标或论文事实。若没有独立 reviewer 可用，按当前上下文做保守本地评审，并将 `reviewer_model` 写成实际模型标识或 `unknown`，不能声称外部评审已执行。

## 与修订循环的关系

`critical_issues` 和 `minimum_next_steps` 是主 agent 的修改输入，应优先指出会改变评分的内容。修订应保持 Idea 的核心问题可识别，避免为了回应意见无限增加模块。每轮评审都要重新判断评分，不得沿用上一轮分数；所有未解决的关键问题必须保留在响应中。

评审完成后，直接返回上述完整 JSON。不要调用 write/edit 写 REVIEW.json 或 REVIEW.md；这两个文件由 workflow 的 `_record_idea_review` 统一保存，避免模型先写原始 JSON 后又被运行器覆盖为不同 schema。

运行器负责产生：

1. `idea-stage/REVIEW.json`：包含 review、selected_idea 和 candidates 的机器可解析状态；
2. `idea-stage/REVIEW.md`：面向研究者的评审意见。
