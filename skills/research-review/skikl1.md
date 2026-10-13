---
name: research-review
description: "从外部审稿人后端获取深入的批判性研究审稿（Codex 或手册）。当用户说“审稿我的研究”、“帮我审稿”、“获取外部审稿”，或者想要对研究想法、论文或实验结果提出批评性反馈时使用。"
argument-hint: "[topic-or-scope]"
allowed-tools: Bash(*), Read, Grep, Glob, Write, Edit, mcp__codex__codex, mcp__codex__codex-reply, mcp__manual_review__review, mcp__manual_review__review_reply
---

# 通过外部审稿人后端研究审稿（超推理）

> 🔒 **不要将这个技能包裹在 `/loop` 、 `/schedule` 或 `CronCreate` 中。** 它是判决性的 - 它会产生跨模型的审稿判决，多轮与审稿人线程连续性。外部计时器重新对挂钟时间做出判决，并打破审稿人的逐轮记忆：零新信号，全部代币成本。安排*之前的外部等待*（工作准备→然后审稿一次），而不是判决。请参阅[`shared-references/external-cadence.md`](../shared-references/external-cadence.md)。

从选定的外部审稿人后端获得具有最大推理深度的多轮研究工作批判审稿。

## 常量

- REVIEWER_MODEL = `gpt-6-astra` — Codex 后端的默认模型，推理工作 `ultra`（深度审核层）。必须是 OpenAI 型号（例如 `gpt-6-astra` 、 `gpt-5.5` 、 `o3` ）。手动后端使用用户选择的模型 - 它必须是来自不同系列的公认模型（ OpenAI ， Anthropic ， Google， DeepSeek ， Moonshot/Kimi ， Qwen ）。
- **REVIEWER_BACKEND = `codex`** — 默认：Codex MCP（超）。对于 Oracle MCP 可以用 `— reviewer: oracle-pro` 覆盖，对于手册审稿 MCP 可以用 `— reviewer: manual` 覆盖。如果没有手册审稿MCP，则停止并打印安装命令；不要回退到 Codex 。参见 `shared-references/reviewer-routing.md`。

## 审稿人调用约定

拨打REVIEWER_BACKEND分行审稿人时：

**如果 REVIEWER_BACKEND = `codex` :** 使用 `mcp__codex__codex` 作为新审稿主题。使用 `mcp__codex__codex-reply` 进行后续回合（重用 threadId）。

**如果 REVIEWER_BACKEND = `manual` :** 使用 `mcp__manual_review__review` 进行新的审稿线程： 提示：[与 Codex 的提示完全相同] config: {"model_reasoning_effort": "xhigh", "executor_model": "<实际执行器模型>", "require_reviewer_model": true} 保存返回的 `threadId` 。使用 `mcp__manual_review__review_reply` 进行后续轮次： threadId: [已保存的手册-审稿 threadId] 提示：[后续提示] config: {"model_reasoning_effort": "xhigh", "executor_model": "<实际执行人模型>", "require_reviewer_model": true}

内容保真度：手册审稿人应该看到相同的实质性审稿简报Codex也会阅读。如果手动UI支持文件上传/附件，则重复使用相同的简要文件；否则粘贴内联简短内容，因为远程 Web UI 无法读取您的本地文件系统路径。 审稿追踪同样适用于两个后端。

## 上下文:  $ARGUMENTS

## 先决条件

- **Codex MCP 服务器** 在 Claude Code 中配置：
  ```bash
  claude mcp add codex -s user -- python3 "$HOME/aris_repo/mcp-servers/codex-exec/server.py"   # 您的 ARIS 克隆路径
  ```
- 这使 Claude Code 可以访问 `mcp__codex__codex` 和 `mcp__codex__codex-reply` 工具

## 工作流程

### 第 1 步：收集研究上下文
在致电外部审稿人之前，请编写一份全面的简报：
1. 阅读项目叙述性文档（例如，STORY.md、README.md、论文草稿）
2. 阅读任何 memory/notes 文件以获取主要发现和实验历史记录
3. 识别：核心论断、方法论、关键结果、已知弱点

### 第二步：初审稿（第一轮）
使用选定的后端发送具有超推理的详细提示。对于 `codex` 后端，请保持 MCP 有效负载简短：将完整的简报写入 `RESEARCH_REVIEW_REQUEST.md` ，然后将 Codex 指向该文件。

*对于法典后端：*

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“超”}
  提示：|
    审稿摘要请阅读<RESEARCH_REVIEW_REQUEST.md的绝对路径>。
    执行者注释并不是他们引用的文件之外的证据，因此请验证
    评判前参考产物。
    请担任高级ML审稿人（NeurIPS/ICML级别）。从
    假设工作在某个地方被破坏了——你的工作就是找到哪里。
    保持敌对态度。不要相信作者告诉你的一切——验证一切
    你自己。确认：
    1. 逻辑漏洞或不合理的论断
    2. 缺少能够强化故事的实验
    3. 叙事弱点
    4. 贡献是否足以满足顶级目标会议或期刊的要求

    === 范围限制（这些范围限制了您的提议，而不是您所寻找的内容）===
    报告任何实际错误的情况 - 包括看起来罕见的案例，如果
    这个仓库实际上产生了它。然后将修复保持在范围内：
    1. 这是一个研究工作流程工具，而不是安全论文。验证是
       欢迎;过度防御则不然。假设有自己的合作运营商
       机器 - 恶意本地用户不在威胁模型中。
    2. 不要建议 SHA / 散列 / 内容指纹 / 摘要绑定方案。
       报告散列代码中已经存在的真正缺陷是可以的。
    3. 无投机机制：不添加功能标志、迁移框架、
       兼容层、包装器、引脚或类似机制，除非有证据表明
       他们修复的当前回购缺陷或他们必须修复的明确现有不变量
       保存。 “承重”、“兼容”、“不搭脚手架”都是标签，
       不是证据。指向失败的 path/artifact 或不变量，并检查
       提案的事实前提，例如是否存在指定的包版本。
    4. 没有极端情况的困扰：外来编码、符号链接竞争、RTL 文本和
       除非您能证明此处出现了这种情况，否则毫秒竞赛超出了范围。
    5. 当真正需要一个标题或清单时，不要过度机械化
       判断。人类读出的清晰句子胜过评分表
       维持。
    例外：运行远程命令、启动网络服务或安装的代码
    MCP 服务器使用其凭据在用户计算机上运行 — 信任边界
    调查结果在范围内并且默认是严格的。
    当某些事情是正确的时，请直白地说出来。不要制造调查结果。
    残酷地诚实。如果在真正尝试打破它之后，工作
    坚持并准备好，说清楚。
```

审稿摘要应包含审稿人应检查的完整研究脉络、具体问题以及主要产物/原始结果路径。

*对于手动后端：*使用`mcp__manual_review__review`，具有相同的简要内容。如果手册审稿UI支持附件，请附上`RESEARCH_REVIEW_REQUEST.md`；否则粘贴简短的内联内容。保存返回的 `threadId` 。

### 第 3 步：迭代对话（第 2-N 轮）
对于 `codex` 后端：将 `mcp__codex__codex-reply` 与返回的 `threadId` 一起使用。对于 `manual` 后端：使用 `mcp__manual_review__review_reply` 和相同的 `threadId` 。使用适当的工具继续对话。对于 Codex 后续轮次，请编写更新的摘要，例如 `RESEARCH_REVIEW_ROUND_2.md` 并仅发送路径：

```text
mcp__codex__codex-reply :
  threadId: [在步骤 2 中保存的审稿人 threadId]
  # 回复继承线程的 model/effort (gpt-6-astra ultra)
  提示：|
    请阅读更新后的审稿摘要，网址为<绝对路径
    RESEARCH_REVIEW_ROUND_2.md >.
    重点关注未解决的弱点以及修订是否真正修复了这些弱点。
```

对于手动后续轮次，如果可能，请附上相同的更新摘要；否则将其内联粘贴。

每轮：
1. **用 evidence/counterarguments 回应**批评
2. **针对最可行的要点提出有针对性的后续行动**
3. **请求具体交付成果**：实验设计、论文大纲、论断矩阵

主要后续模式：
- “如果我们将 X 重新定义为 Y，这会改变你的评估吗？”
- “满足问题Z的最少实验是什么？”
- “请设计最少的附加实验包（每 GPU 周的最高接受提升）”
- “请写一份带有分数的模拟审稿NeurIPS/ICML”
- “给我一个可能的实验结果的结果到论断矩阵”

### 第四步：收敛
出现以下情况时停止迭代：
- 双方就核心论断及其证据要求达成一致
- 制定具体的实验计划
- 叙事结构已经确定

### 第 5 步：记录一切
将完整的交互和结论保存到项目根目录的审稿文档中：
- 逐轮总结批评与回应
- 关于论断、叙述和实验的最终共识
- 论断矩阵（在每种可能的结果下允许什么论断）
- 具有预计计算成本的优先 TODO 列表
- 论文大纲（如果讨论）

更新项目 memory/notes 的关键审稿结论。

> **组合模式** - 如果使用 `— composed: <canonical-report-path>` 调用（像 `/idea-discovery` 这样的编排器通过此），请不要**在项目根目录中编写独立的审稿 `.md`。原始对话已保存至 `.aris/traces/…`（请参阅下面的 *审稿追踪* - 审计副本在每种模式下均保留）；将审稿*结论*（共识、论断矩阵、优先待办事项）折叠到编排者的规范报告中，并引用其中的跟踪路径。**默认（无 `— composed:` 指令）：行为与上述完全相同 - 编写独立的审稿文档。 **切勿从仅存在的报告文件推断组合模式。完整规则：[ `shared-references/output-composition.md` ](../shared-references/output-composition.md)。

## 主要规则

- 审稿时始终固定 `model: gpt-6-astra` + `config: {"model_reasoning_effort": "ultra"}`（深度审核层；根据 `reviewer-routing.md` 进行功能回退，绝不低于 `xhigh` ）
- 该引脚是 **Codex** 后端的。对于 `manual` ，使用上面审稿人调用约定中的身份承载配置；  `model`、`sandbox` 和 `cwd` 仅限 Codex
- 将全面的上下文放在审稿摘要中。  Codex 传递绝对路径时可以读取本地文件；手册审稿人通常不能，所以附上或粘贴相同的摘要。
- 诚实地面对弱点——隐藏它们会导致更糟糕的反馈
- 拒绝你不同意的批评，但接受有效的批评
- 关注可行的反馈——“什么实验可以解决这个问题？”
- 记录 threadId 以供将来恢复使用
- 审稿文件应该是独立的（无需对话即可阅读）

## 提示模板

### 初审稿：
“我将为您的关键审稿提供一个完整的机器学习研究项目。请担任高级机器学习审稿人（NeurIPS/ICML 级别）......”

### 对于实验设计：
“请设计最少的附加实验包，使 GPU 每周的接受度提升最高。我们的计算：[描述]。配置要非常具体。”

### 对于论文结构：
“请将其转化为具体的论文大纲，并附上逐节的论断和图形计划。”

### 对于论断矩阵：
“请给我一个结果到论断矩阵：在实验 X 和 Y 的每种可能结果下允许什么论断？”

### 模拟审稿：
“请写一份模拟 NeurIPS 审稿，内容包括：摘要、优点、缺点、作者提问、分数、信心以及如何接受。”

## 审稿溯源

每次审稿人调用（`mcp__codex__codex`、`mcp__codex__codex-reply`、`mcp__manual_review__review`或`mcp__manual_review__review_reply`）后，保存以下`shared-references/review-tracing.md`的跟踪（策略C——取证；永远不要默默地跳过）。使用 `save_trace.sh` （根据 `shared-references/integration-contract.md` §2 中的链进行解析）或直接将文件写入 `.aris/traces/<skill>/<date>_run<NN>/` 。遵循 `--- trace:` 参数（默认值： `full` ）。带有判决的手动响应必须以 `Reviewer-Model: <exact-model-id>` 开头 - 在 `executor_model` 中填写本次会话实际运行的模型。缺失、未知或同一模型家族的身份不能作为判定依据；发出 `REVIEW_UNAVAILABLE` 而不是猜测。如果不能说出执行者模型，审稿由不同模型家族完成的论断就无法证明——在报告中这样说，而不是断言。

