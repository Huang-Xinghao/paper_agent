# 方案审阅技术说明

用户流程见 [方案审阅使用说明](../guides/proposal-review.md)。本模块位于 `research_refine` 与 `experiment_plan` 之间，工作流节点名为 `refine`；`proposal_revision` 是专用修订任务阶段，避免与重新跑整个研究精炼混淆。

## 数据归属

| 项目内文件 | 用途 |
| --- | --- |
| `refine-logs/FINAL_PROPOSAL.md` | 研究精炼原提案；批准时更新为正式交付正文。 |
| `refine-logs/IDEA_CANVAS.json` | 人编辑及模型统一修订的草稿，包含 `review.sections`、节点、连接和源提案 hash。 |
| `refine-logs/IDEA_REVIEW.json` | 当前修订记录：摘要、直接及关联影响、问题、覆盖关系、版本与批准信息。 |
| `refine-logs/review-history/{run_id}.json` | 各轮提交、候选及修订记录，便于追溯。 |
| `refine-logs/approved-proposals/{formal_hash}.md` | 人批准后的锁定正文。 |

模型修订返回的 `review.sections` 是统一正文，各 section 的 `details` 保存完整说明，`summary` 提供易读摘要。方法节点通过 `sectionIds` 引用正文，`impacts`、`questions`、`coverage` 同样关联章节。历史 MD 的前端投影只是兼容入口；不能因此声称上游已经按讨论稿生成完整结构化协议。

## 保存 → 修订 → 批准

`api/idea_canvas.py` 提供 `GET/PUT /api/projects/{project_id}/idea-canvas`。保存携带已读草稿 `sha256` 及 `sourceHash`；校验项目路径、文件边界、运行状态和源文件是否改变。采用 Linux 文件锁、数据库检查及临时文件替换，避免覆盖运行中的产物。草稿上限 2 MiB，保存不推进研究流程。

提交 `proposal_revise` 任务后，`api/proposal_revision.py:run_revision()`：

1. 验证提交版本，读取原提案、上轮修订和人工草稿；保存本次模型输入。
2. 固定 `proposal_revision` Skill，附加研究指导，再重申代码内固定协议。单次流式模型请求不提供工具。
3. 接收时报告 `preparing/waiting_model/receiving/validating/saving/ready`；进度事件节流，避免每个 token 写一次 SQLite。未完整结束不接受候选，保留响应用于排查。
4. 使用 Pydantic 验证结构、关联引用、问题标识和原提案二级标题覆盖。要求反馈已整合，不能用短摘要替换全文。格式验证不能证明语义质量，人仍需复审。
5. 再检查源提案、工作流和提交草稿是否变化，通过后写入新草稿、修订记录与历史。修订本身不批准、不执行实验。

`proposal_approve` 任务通过 `proposal_review.py:require_candidate()` 核验：候选与当前草稿对应、没有待决问题、未批准，并由原工作流核验专家研究精炼检查点。批准时 `render_proposal()` 从 section 完整正文生成 MD，保存 hash 对应的锁定副本，更新批准记录，再进入实验规划。批准后不再调用模型重新改写正文。

`guard_legacy_approval()` 约束旧审批入口，避免绕过已有的新审阅草稿。自动模式保持原逻辑；Skill 文本无权取消这些检查。

## 修改这个模块时注意什么

- 摘要、画布与正文是同一方案的投影。新增交互必须落实到可提交的草稿，并由统一修订整理，不要制造独立的“执行方案副本”。
- 图连接只能表达已声明的关联；实际内容影响交给修订 Agent 分析，再由人复审，不能把连线命中等同语义正确。
- 当前问题非空即阻塞批准；[上游输出约定讨论稿](../proposal-output-contract-draft.md) 中的分级委派尚未实施。
- 涉及草稿格式时同步维护 `proposal_review.py` 的模型、前端类型和旧 MD 兼容；涉及阶段 Skill 时同步验证快照。
- 核心回归：`python -m pytest tests/test_proposal_review.py tests/test_idea_fixes.py`。浏览器测试使用 `tests/proposal_review_demo.py` 的独立模拟服务，前端测试先检查专属标记，避免对真实项目操作。

源码：[数据与批准校验](../../paper_agent/proposal_review.py) · [草稿 API](../../paper_agent/api/idea_canvas.py) · [修订调用](../../paper_agent/api/proposal_revision.py)
