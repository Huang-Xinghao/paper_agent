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
- 角色会话的图片字节按内容哈希单独保存到 `.paper-agent/images/`，会话 JSON 只保存引用。仅当前尚未消费的图片在请求模型时加载，历史图片及摘要保留文字引用。旧版内嵌图片在恢复会话时转换，保留有效摘要的覆盖范围；普通模型请求前不重复保存未变化的会话。
- 论文评审由运行器将当前 PDF 渲染为带页码的四页联系图，通过 `codex exec --image` 直接传入，另存单页 PNG 供放大检查。评审提示禁止在 shell 输出图片 base64；无法查看图片时必须说明检查缺口。启用诊断时预览随本次评审保留，否则使用临时目录并在结束后清理。渲染占用同一评审超时预算。
- 超参数寻优只从 `refine-logs/EXPERIMENT_PLAN.md` 读取执行策略，不回退到研究方案中的旧策略。
- 论文原始 PDF 统一保存在 `research-wiki/literature/raw-paper/`（随 `PAPER_AGENT_WIKI_ROOT` 配置），文献发现与 OpenAlex/arXiv 下载共用此目录。独立研究项目可读取共享 PDF，普通文件写入工具仍限于项目内。删除根目录旧 Zotero 缓存与依赖失效同步清单的 `backfill_wiki_assets.py`；合并记录保存在本地 `.paper-agent/library-consolidation/`。

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

网页方案总览与方法画布合为模型方案页：上方架构图，下方仅展示模型输入、架构、输出。内容从正式 `FINAL_PROPOSAL.md` 读取，不依赖历史画布草稿，也不为展示调用模型。新方案以对应的三个二级标题提供内容。自动模式展示不形成审批关卡，继续授权范围内的下一阶段；专家模式在方案下方回答主控给出的问题，也可直接确认当前方案。纯同意回复不触发修订，包含实际变更的回答仍按专家修改流程处理。内部方案修订 JSON 只保留在运行产物中，不作为对话消息展示。

保留真实执行回执、任务身份与取消归属、依赖图校验、产物校验、实验资源与预算限制、已批准方案冻结、并发编辑保护、论文编译及引用来源校验。这些约束防止重复执行和误用结果。两轮论文评审都检查当前稿件，不输入旧评审或改进日志；修订 Agent 只实施评审意见，评审调用由运行器负责。

## 验证范围

项目测试覆盖服务边界、MCP SDK 本地 stdio 会话、图片成功/失败、流式中断、方案并发编辑、自由结构交付、用户批准、实际进程回执、实验续跑以及两轮论文评审。使用本地模拟服务和临时文件，不验证真实提供商的在线响应，也不启动科研训练。

## Wiki 来源分区

外部文献归入 `research-wiki/literature/`，本项目自主研究归入 `research-wiki/autoresearch/`。两边各自保存 `papers/`、`ideas/`、`experiments/`、`claims/`、`raw-paper/`、`sources/`、`figures/`、`metadata/`。`sources/` 保存解析后的逐页原文和证据块，`metadata/<slug>/` 保存来源、质量审阅、配图、参考文献和关系处理状态。来源分区不表示发表状态。

根目录的 `graph/edges.jsonl` 维持一张共享关系图，节点 ID 不变，可以跨分区关联。`index.md` 和 `log.md` 也由全库共享。旧版 `papers/<slug>.md` 等路径在读取时自动定位到唯一分区，避免历史任务链接失效。新库可使用 `paper-wiki init <wiki_root>` 初始化。前端分类显示来源和条目类型，关系图读取所有活动节点与关系。自主研究的最终 PDF 同时归档到其 `raw-paper/`，不依赖项目目录的相对路径。

Wiki 文件名从第一作者的罗马字姓名提取姓氏：`Chen Ma (马晨)` 使用 `ma`，正文作者信息仍保留原文。2026-11-16 修正了 5 篇双语作者条目及派生节点的旧命名，历史文件路径由共享 `graph/path_aliases.json` 映射到新文件，不建立重复活动节点。
