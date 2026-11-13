# 运行代码整理（2026-11-13）

本轮整理运行代码和调用约定。`paper_agent/skills/` 中的旧说明暂时保留，运行器在任务提示中明确当前交付要求与评审分工。

## 统一入口

| 职责 | 唯一实现 | 调用方式 |
| --- | --- | --- |
| 模型请求、流式解析、用量记录 | `integrations/model.py` | Agent、讨论和方案修订共用 `ModelClient`；方案修订单次调用，不重放中断请求 |
| 外部 MCP | `integrations/mcp.py` | 官方 SDK 管理 HTTP/stdio 会话、发现工具；实验 Agent 与实验室页面共用 |
| 外部评审 | `integrations/review.py` | `ReviewRunner`；想法/代码工具、论文工作流、定向审稿、`paper-review` CLI 共用 |
| 图片 API | `integrations/images.py` | `generate_image` 工具直接调用；单次请求、校验 PNG、原子保存 |
| 原子文件写入 | `storage.py` | JSON、方案画布、评审报告、图片和会话共用 |
| 阶段定义与基础验收 | `agent/stages.py` | 阶段顺序、角色、必要输入/输出、用户审阅文件 |

删除了旧模块路径及转发层，不提供旧 import 或旧命令兼容。MCP 配置只使用 `http`、`stdio`；评审程序通过 `PATH` 或 `CODEX_BIN` 指定。MCP 协议允许的结构化结果和 JSON 文本结果都可读取。

删除了专用 BTS HTTP 客户端、重复工具包装、自制图片 MCP 客户端/服务、已无调用方的 Wiki 写入 CLI、专用威胁扫描及元数据迁移脚本。已有 Wiki 查询、引用校验、本地 PDF 和图片检索继续使用。Wiki 元数据只读取 `metadata/<论文名>/<类型>.json`。

## 文件位置

- 正式研究产物沿用 `stages.py` 的目录：`literature/`、`data-search/`、`idea-stage/`、`refine-logs/`、`figures/`、`paper/`。
- 想法评审：`idea-stage/review.md`；代码评审：`refine-logs/CODE_REVIEW.md`；论文两轮评审：`paper/review_round1.md`、`paper/review_round2.md`。
- 外部评审诊断统一放在 `.paper-agent/reviews/<类型>/<报告名>/<调用编号>/`，不混入论文产物。
- 会话、工具调用、用量和产物版本记录放在 `.paper-agent/`。实验进程日志及执行回执保留在专用 `experiment-runs/`，科学产物放在 `results/`；工作流与队列状态由运行器维护。
- 超参数寻优只从 `refine-logs/EXPERIMENT_PLAN.md` 读取执行策略，不回退到研究方案中的旧策略。

## 放宽的验收

必要交付文件必须存在、非空且位于项目内；文档结构和工作文件由 Agent 决定。

- 删除图表专用的视觉 JSON、逐图哈希、API 失败计数和强制回退证明。
- 删除角色额外生成的 `EXPERT_CHECKPOINT.json`，以及固定问题数量、逐字摘录和二次摘要工具的强制门槛；主控可直接解读交付，也可自愿更新检查点摘要。
- 写论文只固定 `paper/main.tex` 主入口，不强制章节目录或参考文献文件名。
- 实验不要求 Markdown 使用 D/B 标题；`block_id`、`required_inputs`、`depends_on` 可按实际需要填写，省略依赖表示独立任务。tracker 可以按 manifest 任务生成。
- 论文规划读取 manifest 声明且已有完成回执的产物，支持非 CSV/JSON 结果；不再递归扫描任意数值文件。
- 方案修订可合并或重组章节，删除逐标题覆盖映射。
- 产物变化仅提示 `changed_files`，不自动宣判全部下游失效；Agent 根据实际依赖决定更新范围。

## 保留的约束

专家模式等待用户明确批准当前版本；自动模式可连续执行。用户要求的明确修订会使对应版本重新待审。

保留真实执行回执、任务身份与取消归属、依赖图校验、产物校验、实验资源与预算限制、已批准方案冻结、并发编辑保护、论文编译及引用来源校验。这些约束防止重复执行和误用结果。两轮论文评审都检查当前稿件，不输入旧评审或改进日志；修订 Agent 只实施评审意见，评审调用由运行器负责。

## 验证范围

项目测试覆盖服务边界、MCP SDK 本地 stdio 会话、图片成功/失败、流式中断、方案并发编辑、自由结构交付、用户批准、实际进程回执、实验续跑以及两轮论文评审。使用本地模拟服务和临时文件，不验证真实提供商的在线响应，也不启动科研训练。
