---
name: auto-paper-improvement-loop
description: "通过GPT-6-Astra xhigh审稿自主改进生成的论文→实施修复→重新编译，2轮。当用户说“改论文”、“改进论文”、“论文润色循环”、“自动改进”或想要迭代完善生成的论文时使用。"
argument-hint: "[paper-directory] [— style-ref: <source>] [— edit-whitelist <path>]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, Skill, mcp__codex__codex, mcp__codex__codex-reply
---

# 自动论文改进循环：审稿→修复→重新编译

> 🔒 **不要将此技能包装在 `/loop`、`/schedule` 或 `CronCreate` 中。** 它已在内部执行“审稿 → 修复 → 重新编译”的循环，具有自己的轮次结构，并且每轮都会启用独立审稿人偏差防护（不使用 `codex-reply`）。按时间反复要求它“改进论文”不会产生新的信号：质量取决于审稿内容的变化，而非时间流逝。如果定时重跑还根据自己的输出决定何时停止，就会越过策略边界（`acceptance-gate.md`）。应为循环之前的外部等待安排定时任务。参见 [`shared-references/external-cadence.md`](../shared-references/external-cadence.md)。

自主改进论文：**$ARGUMENTS**

## 上下文

该技能设计为在工作流程 3 **之后**运行（ `/paper-plan` → `/paper-figure` → `/paper-write` → `/paper-compile` ）。它需要一份已编译的论文，并通过外部 LLM 审稿对其进行迭代改进。

与`/auto-review-loop`（迭代**研究**——运行实验、收集数据、重写叙述）不同，该技能迭代**论文写作质量**——修复理论不一致、软化过度主张、添加缺失的内容和改进表达方式。

## 常量

- **MAX_ROUNDS = 2** — 两轮审稿→修复→重新编译。根据经验，第 1 轮捕获结构问题 (4→ 6/10 )，第 2 轮捕获剩余的表达问题 (6→ 7/10 )。对于仅改善写作的任务，超过 2 轮的回报递减。
- **REVIEWER_MODEL = `gpt-6-astra`** — 通过 Codex MCP 用于论文审稿的模型。
- **REVIEWER_BIAS_GUARD = true** — 当 `true` 时，每轮审稿都使用新的 `mcp__codex__codex` 线程，没有先前的审稿上下文。审稿时切勿使用`mcp__codex__codex-reply`。设置为 `false` 仅用于有意调试遗留行为。 **经验证据：**用`codex-reply`运行相同的论文+“自上一轮以来我们做了X”提示会使多轮评分从真实的 3/10 虚增至 8/10；切换到新线程恢复了真实的 3/10 评估。
- **REVIEW_LOG = `PAPER_IMPROVEMENT_LOG.md`** — 所有回合的累积日志，存储在论文目录中。
- **HUMAN_CHECKPOINT = false** — 当 `true` 时，每轮审稿后暂停并向用户呈现分数+弱点。用户可以批准修复、提供自定义修改说明、跳过特定修复或提前停止。当`false`（默认）时，完全自主运行。
- **EDIT_WHITELIST = `null`** — YAML/JSON 白名单文件的可选路径，限制修复实施步骤可能涉及的路径和操作。当 `null`（默认）时，所有编辑都不受限制地进行。当通过 `— edit-whitelist <path>` （也接受 `— edit_whitelist <path>` ）设置时，循环在启动时加载文件并在每次编辑之前查阅它；被拒绝的编辑会记录到 `PAPER_IMPROVEMENT_LOG.md`，而不是默默地删除。请参阅下面的“可选：编辑白名单”。

> 💡覆盖：`/auto-paper-improvement-loop "paper/" — human checkpoint: true`

## 可选：样式参考（ `— style-ref: <source>` ，选择加入）

让用户在改进过程中仅进行结构修复（章节重新排序提示、段落长度微调、图形密度调整）以参考论文。 **默认关闭——当用户没有通过 `— style-ref` 时，与之前不做任何不同的事情。**

仅当 `— style-ref: <source>` 出现在 `$ARGUMENTS` 中时，才运行辅助程序，然后循环开始：

```bash
# 通过规范的严格安全链解析 $STYLE_HELPER（参见
# shared-references/integration-contract.md §2)。政策 A — 门：
# unresolved 辅助程序意味着 --style-ref 无法满足，因此中止。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
STYLE_HELPER=".aris/tools/extract_paper_style.py"
[ -f "$STYLE_HELPER" ] || STYLE_HELPER="tools/extract_paper_style.py"
[ -f "$STYLE_HELPER" ] || { [ -n "${ARIS_REPO:-}" ] && STYLE_HELPER="$ARIS_REPO/tools/extract_paper_style.py"; }
[ -f "$STYLE_HELPER" ] || {
  echo "ERROR: extract_paper_style.py not resolved at .aris/tools/, tools/, \$ARIS_REPO/tools/, or via ~/.aris/repo." >&2
  echo "       Fix: rerun bash tools/install_aris.sh or smart_update.sh (refreshes ~/.aris/repo), export ARIS_REPO, or copy the helper to tools/." >&2
  echo "       --style-ref cannot be satisfied; aborting." >&2
  exit 1
}
STYLE_STATUS=0
CACHE=$(python3 "$STYLE_HELPER" --source "<source>") || STYLE_STATUS=$?
case "$STYLE_STATUS" in
  0) ;;                                       # 仅使用 $CACHE / style_profile.md 作为 FIX 阶段的结构指导
  2) echo "warning: style-ref skipped (missing optional dep)" >&2 ;;
  3) echo "error: --style-ref source failed; aborting loop" >&2 ; exit 1 ;;
  *) echo "error: helper failed unexpectedly; aborting loop" >&2 ; exit 1 ;;
esac
```

接受的来源：本地 TeX 目录/文件、本地 PDF 、arXiv id、http(s) URL。  Overleaf URLs/IDs 被拒绝 — 首先通过 `/overleaf-sync setup <id>` 克隆并传递本地克隆路径。

**严格的规则**（`tools/extract_paper_style.py` 文档字符串中的完整合同）：

- 仅在**修复实施**阶段使用 `style_profile.md`，以便在应用审稿人反馈时推动结构选择。 审稿人的反馈永远优先；样式引用是“如何”应用修复的决定因素，而不是“是否”应用修复的决定因素。
- **在实施修复时，切勿从可通过缓存访问的任何内容中复制散文、论断、示例或术语**。
- **切勿将`— style-ref`（或缓存内容）传递给GPT-6-Astra审稿人分代理。**下面的审稿人独立协议要求审稿人只能看到成果和用户的提示——泄露样式引用会污染审稿人端的系统。 **这是该技能中最关键的不变量。**

## 可选：编辑白名单（ `— edit-whitelist <path>` ，选择加入）

让调用者硬约束允许接触哪些文件和操作**修复实现**步骤（步骤 3 / 步骤 6）。 **默认关闭 - 当用户未通过 `— edit-whitelist` （或别名 `— edit_whitelist` ）时，循环应用所有审稿人驱动的编辑，不受限制，与以前完全相同。**

这是上游管道（例如 `/resubmit-pipeline` 第 2 阶段）用于强制仅文本重新提交微编辑的参数：没有 `.bib` 突变，没有 `.sty` / `.bst` 突变，没有对先前提交目录的编辑，没有新的 `\cite{...}` ，没有新的定理环境，没有新的数值断论。

### 数据结构

白名单文件为 YAML 或 JSON 。所有四个部分都是可选的：

```yaml
allowed_paths:
  - sec/*.tex
  - main.tex
  - figures/*.tex
forbidden_paths:
  - "**/*.bib"
  - "**/*.sty"
  - "**/*.bst"
  - "../OldSubmission/**"
forbidden_operations:
  - new_cite              # 块 \cite{...}、\citep{...}、\citet{...}、\citeauthor{...} 添加
  - new_bibitem           # 块 \bibitem{...} 添加
  - new_theorem_env       # 块 \begin{定理|引理|命题|推论} 添加
  - numerical_claim       # 阻止添加新数字/百分比/指标
forbidden_deletions:      # 阻止删除而不是添加的操作
  - delete_existing_cite  # 阻止从正文中删除 \cite{...} （使用 itation-audit --soft-only 代替）
  - delete_theorem_env    # 阻止删除现有的 \begin{theorem|...} 块
requires_user_approval_for:  # 不自动拒绝但暂停以明确用户确定的操作
  - rewrite_abstract      # 解释整个摘要会触发检查点
  - rewrite_intro_first_para
  - delete_section
max_edits_per_round: 30   # 每轮接受的编辑数量有硬性上限（被拒绝的编辑不计入；如果达到上限，剩余的建议编辑将推迟到下一轮并发出警告）
rationale: "Resubmit mode: text-only microedits, paper structure frozen by user constraint."
```

### 决议规则

- **`allowed_paths` 为空且 `forbidden_paths` 为空** → 白名单是无操作（建议：文件已加载且 `rationale` 回显到日志，但不应用路径过滤）。
- **`allowed_paths` 为空，`forbidden_paths` 非空** → 所有与 `forbidden_paths` 不匹配的路径都是可变的。
- **`allowed_paths` 非空，`forbidden_paths` 空** → 只有匹配 `allowed_paths` 的路径是可变的。
- **两者都非空** → 如果目标匹配 `allowed_paths` 并且不匹配 `forbidden_paths` ，则允许编辑。  `forbidden_paths` 总是在重叠时获胜。
- **`forbidden_operations` 缺失或为空** → 无操作级防护；仅适用路径级过滤。

### 通配模式语义

使用 bash `extglob` / Python `fnmatch.fnmatch` 语义。  `**` 匹配任何深度（零个或多个目录段）。模式与相对于论文目录**的路径进行匹配（例如，当论文目录是 `paper/` 时，`paper/sec/intro.tex` 匹配 `sec/*.tex` ）。

### 禁止操作探测器

对于每个候选编辑的差异（添加的新行 - 删除除外），运行这些正则表达式的循环会检查并拒绝是否有任何禁止的操作匹配：

| 操作 | 检测器（仅添加线路） |
|-----------|------------------------------|
| `new_cite` | `\\cite[a-zA-Z]*\{[^}]+\}`（捕获`\cite`、`\citep`、`\citet`、`\citeauthor`、`\citeyear`、`\citealp`等） |
| `new_bibitem` | `\\bibitem\{[^}]+\}` |
| `new_theorem_env` | `\\begin\{(theorem|lemma|proposition|corollary)\*?\}` |
| `numerical_claim` | 与 `\b\d+(\.\d+)?%?\b` 匹配的新令牌未出现在 deleted/replaced 行中（即真正的新数字，而不是对现有数字的编辑） |

### 循环开始时的行为（第一轮修复实施之前）

1. 如果`— edit-whitelist <path>`存在于`$ARGUMENTS`中，则设置`EDIT_WHITELIST = <path>`。
2. 加载文件（ `yaml.safe_load` ；如果失败，则回退到 `json.loads` ）。加载失败时，中止循环并显示明显错误 - 不要默默地不受约束地继续。
3. 在新的“编辑白名单”序言部分下将 `rationale`（如果存在）回显到 `PAPER_IMPROVEMENT_LOG.md`，以便审计跟踪记录编辑受到限制的原因。

### 修复实施期间的行为（步骤 3 和 6）

在应用每个建议的编辑之前：

1. 解析相对于论文目录的目标文件路径。
2. 路径检查：如果 `allowed_paths` 非空，则目标必须至少匹配一种模式。然后，如果 `forbidden_paths` 非空，则目标不能匹配任何模式。如果任一失败 → 因 `path` 违规而拒绝。
3. 操作检查：为建议的编辑构建统一的差异（或只是一组新添加的行）。对于 `forbidden_operations` 中的每个条目，在添加的行上运行其检测器。如果任何检测器匹配 → 因 `operation` 违规而拒绝。
4. 如果所有检查都通过，则正常应用编辑。
5. 如果被拒绝，请在 `## Rejected by edit_whitelist (Round N)` 标题下向 `PAPER_IMPROVEMENT_LOG.md` 附加一个条目，数据结构如下：
   ```
   - 文件：<相对路径>
     原因：路径| 操作
     模式：<违规的forbidden_path通配模式，或者违规的forbidden_operation名称+匹配的子字符串>
     reviewer_concern：<促使本次编辑的原始第 N 轮弱点>
   ```
6. 继续本轮剩余的编辑。不要因一次拒绝而中止整轮。

### 每轮结束时展示拒绝记录

在每轮结束时（重新编译之后，进入下一轮之前），如果在该轮的修复步骤中有任何编辑被拒绝：

- 将一行摘要打印到该轮的检查点输出：`Edit whitelist rejected N edits this round (M path, K operation). See PAPER_IMPROVEMENT_LOG.md "Rejected by edit_whitelist (Round N)".`
- 如果是 `HUMAN_CHECKPOINT = true` ，则在用户批准下一轮修复之前，将拒绝列表包含在向用户显示的检查点中。

### 调用示例

```bash
# 重新提交流程第 2 阶段调用者（纯文本模式）：
/auto-paper-improvement-loop "paper/" — edit-whitelist .resubmit/edit_whitelist.yaml

# 接受别名形式：
/auto-paper-improvement-loop "paper/" — edit_whitelist .resubmit/edit_whitelist.yaml

# 与其他标志组合：
/auto-paper-improvement-loop "paper/" — human checkpoint: true — edit-whitelist constraints.yaml
```

### 基本原理

如果没有白名单，循环的审稿人驱动的修复步骤可以自由添加引文、引入新的定理环境或调整数值论断——所有这些对于首次提交润色来说都是合理的，但在重新提交/最终提交版/仅反驳模式中被禁止，其中论文结构被外部约束冻结。通过一流参数路由这些约束（而不是依赖 LLM 来“记住”不要执行这些约束）使约束可以强制执行、可通过 `PAPER_IMPROVEMENT_LOG.md` 进行审核，并且在每轮检查点对用户可见。

## 输入

1. **编译论文** — `paper/main.pdf` + LaTeX 源文件
2. **所有`.tex`部分文件** — 为审稿提示而串联

## 状态持久化（紧凑恢复）

如果上下文窗口在循环中填满，Claude Code 会自动压缩。为了恢复，该技能在每轮后写入 `PAPER_IMPROVEMENT_STATE.json`：

```json
{
  "current_round": 1,
  "threadId": "019ce736-...",
  "last_score": 6,
  "status": "in_progress",
  "timestamp": "2026-03-13T21:00:00"
}
```

**启动时**：如果`PAPER_IMPROVEMENT_STATE.json`与`"status": "in_progress"`同时存在且`timestamp`在24小时内，则读取+`PAPER_IMPROVEMENT_LOG.md`恢复上下文，然后从下一轮继续。否则（文件不存在、 `"status": "completed"` 或超过 24 小时），请重新开始。

**每轮之后**：覆盖状态文件。 **完成后**：设置 `"status": "completed"` 。

## 审稿人独立协议

审稿人每一轮都必须是上面的天真。前一轮总结、修复列表和执行者解释不是证据；它们是确认偏差的一个来源。如果审稿人被告知发生了什么变化，即使稿件本身没有实质性改进，分数也往往会上升。

规则：
- 每轮都以 `mcp__codex__codex` 开始，而不是 `mcp__codex__codex-reply` 。
- 切勿将先前的 threadId 传递到下一个审稿提示中。
- 审稿人提示中切勿包含“自上轮以来”、“我们已修复”、“申请后”或任何修复摘要。
- 唯一可接受的改进证据是当前的 `.tex` 源代码和编译的 PDF 。
- 如果在文件中无法观察到修复情况，则不应告知审稿人发生了这种情况。
- 如果需要恢复元数据，则存储返回的threadId，仅用于崩溃恢复；不要用它来保存审稿上下文。

仅当您明确希望使用遗留的上下文携带行为进行调试时，才设置 `REVIEWER_BIAS_GUARD = false`。

## 工作流程

### 第0步：保留原件

```bash
cp paper/main.pdf paper/main_round0_original.pdf
```

### 第 1 步：收集论文文本

将所有节文件连接成一个文本块以用于审稿提示：

```bash
# 按顺序收集所有部分
for f in paper/sections/*.tex; do
    echo "% === $(basename $f) ==="
    cat "$f"
done > /tmp/paper_full_text.txt
```

### 步骤2：第一轮审稿

将论文全文并编译后的PDF发送至GPT-6-Astra xhigh：

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    您正在审阅一篇[目标会议或期刊]论文。请提供详细、结构清晰的审稿。

    判断是否断定两个方向上的校准。建议仅在以下情况下缩小范围：
    当前的范围或方式超出了证据范围；不要要求额外的对冲
    围绕支持的结果。旗帜堆叠树篱，自卫（“我们不
    论断”）、指示声明（“我们不涉及 X”）以及一般警告
    外部限制作为需要删除的书写缺陷。音调修正绝不能改变
    事实、否定、形式、范围、比较方向或数字。
    还标记叙述缺陷：进度报告结构（“我们首先尝试
    A，然后 B")，一个建立在方法丢失的度量基础上的故事，叙述结果
    作为失败（“表现不佳”、“未能超越”）而不是解释为
    目标差异或权衡、没有争论义务的实验、
    摘要或介绍以背景或实施为开端
    而不是问题 -> 差距 -> 想法 -> 最强结果和结论
    最终以新的自我否定结束。解决办法是重新构建并裁剪
    证据支持重构；陈述了真正的弱点
    中立并保持在限制中。切勿删除其中不利的号码
    桌子，并且永远不要将弱点作为权衡。

    ## 论文文件：
    - LaTeX 来源：[列出所有节 .tex 文件]
    - 编译 PDF : paper/main.pdf
    - 图：[列表图文件]

    阅读 LaTeX 源代码（对于 content/logic ）和编译后的 PDF （对于视觉传输方式）。

    ## 审稿须知
    请担任高级ML审稿人（[目标会议或期刊]级别）。提供：
    1. **总分**（1-10，其中6 = 弱接受，7 = 接受）
    2. **总结**（2-3句话）
    3. **优势**（项目符号列表，排名）
    4. **弱点**（项目符号列表，排名：关键>主要>次要）
    5. **针对每个 CRITICAL/MAJOR 弱点**：具体的、可操作的修复
    6. **缺少参考文献**（如果有）
    7. **视觉审稿**（来自PDF）：
       - 图形质量：可读？标签清晰易读吗？灰度中可以区分颜色吗？
       - 图标题对齐：每个标题是否与其图匹配？
       - 布局：孤立的标题、尴尬的分页符、远离参考文献的数字？
       - 表格格式：对齐的列、一致的小数、粗体以获得最佳结果？
       - 视觉一致性：所有图形都使用相同的配色方案？
    8. **结论**：准备好提交了吗？是/几乎/否

    重点：理论严谨性、论断与证据的一致性、写作清晰度、
    自包含性、符号一致性和视觉呈现方式质量。
```

保存第 2 轮的 threadId。

### 步骤 2b：人工检查点（如果启用）

**如果 `HUMAN_CHECKPOINT = false` 则跳过。**

呈现审稿结果并等待用户输入：

```
📋第一轮审稿完成。

得分：X/10 — [判决]
主要弱点（按严重程度）：
1. [批判的] ...
2. [主要的] ...
3. [次要的] ...

回复“go”以实施所有修复，给出自定义说明，“skip 2”以跳过特定修复，或“stop”以结束。
```

解析用户响应与 `/auto-review-loop` 相同：批准/自定义指令/跳过/停止。

### 第 3 步：实施第一轮修复

解析审稿并按严重程度实施修复：

**优先顺序：**
1. 关键修复（假设不匹配、内部矛盾）
2. 主要修复（过度声明、缺失内容、符号问题）
3. 小修复（如果时间允许）

**编辑白名单门（如果设置）：** 如果设置了 `EDIT_WHITELIST`，则在应用每个建议的编辑之前，请根据“可选：编辑白名单”部分检查 `allowed_paths` / `forbidden_paths` 的目标路径以及与 `forbidden_operations` 的换行符差异。拒绝记录在 `## Rejected by edit_whitelist (Round 1)` 下的 `PAPER_IMPROVEMENT_LOG.md` 中，包含文件、原因（`path` 或 `operation`）、违规模式和原始审稿人关注点。循环继续进行剩余的编辑——拒绝永远不会中止这一轮。在回合结束时显示拒绝摘要。

**在应用任何修复之前：**根据证据校准论断并直接陈述它们；一般警告仅属于限制；写作说明绝不是手稿内容；语气编辑永远不会改变论文所知道的内容。

**常见修复模式：**

| 问题 | 修复模式 |
|-------|-------------|
| 假设模型不匹配 | 重写假设以匹配模型，添加正式命题来弥补差距 |
| 真正的夸大其词 | 将论断本身缩小为受支持的 scope/modality — 切勿用听起来更柔和的同义词来代替固定范围、比较或聚合 |
| 谨慎支持论断 | 去除多余的树篱；保留任何使论断为真的范围限定符 |
| 分散的通用警告 | 合并到限制并删除重复项 |
| 故事建立在失败的指标上，或者以失败来叙述结果 | 围绕论文获胜的竞赛重新构建；当证据支持时，将差距解释为目标差异或权衡，否则中立地陈述并缩小论断；保留表中的每个数字 |
| 没有争论义务的实验 | 剪切、缩短、移至附录或重新设计，以证明方法、机制、目标场景价值，或排除替代方案 |
| 缺少指标 | 添加包含真实参数计数和警告的定量表 |
| 定理不是独立的 | 添加“解释”段落，列出所有依赖项 |
| 符号混乱 | 全局重命名冲突的符号，添加符号段落 |
| 缺少参考文献 | 添加到 `references.bib` ，在适当的位置引用 |
| 理论与实践的差距 | 明确地将理论框架化为理想化的；添加综合验证小节 |
| 证明差距（理论论文） | 如果PROOF_AUDIT.md还不存在，则运行`/proof-checker`；修复 FATAL/CRITICAL 问题 |
| 书写混乱/被动语态 | 应用 sciwrite 5 遍审核：杂乱提取 → 主动语态 → 句子结构 → 关键字一致性 → 数字完整性。请参阅 `paper-write` 步骤 5 |
| 数字不匹配（论文与结果） | 如果PAPER_CLAIM_AUDIT.md不存在，则运行`/paper-claim-audit`；修复任何 `number_mismatch` 或 `aggregation_mismatch` 论断 |
| 关键词不一致 | “香蕉规则”：如果方法说“肥胖组”，结果不得说“较重组”。提取关键术语，验证所有部分的一致性 |

### 第 4 步：重新编译第 1 轮

```bash
cd paper && latexmk -C && latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
cp main.pdf main_round1.pdf
```

验证：0 个未定义的参考文献，0 个未定义的引用。

### 步骤 4.5：重述回归测试

每次重新编译后，重新运行定理陈述一致性检查，以便修复轮次不会重新引入附录漂移。 **在步骤 4 之后运行此命令，并在最终格式检查之前在步骤 7 之后再次运行此命令。**

**范围**
- 仅比较 theorem/lemma/proposition/corollary 语句，而不比较证明主体。
- 按`main.tex`输入顺序对文件进行分类：`\appendix`之前的文件为主体； `\appendix`之后的文件为附录。

**标准化比较逻辑**
- 去掉注释、 `\label{...}` 、 `\ref{...}` 、 `\eqref{...}` 、 `\cite...{...}` 和仅空白的差异。
- 将仅格式化宏（例如 `\emph{}` 、 `\textbf{}` 、 `\textit{}` 、 `\mathrm{}` 、 `\mathbf{}` 、 `\mathcal{}` 和 `\operatorname{}` ）折叠到其内容。
- 保留量词、大小写分割、假设和已定义对象的字面名称。
- 通过可用的定理标签进行比较；否则按定理类型和顺序进行比较。
- 将假设、案例分割、量词顺序或术语（ `stationary` 与 `terminal` ）中的任何变化标记为回归漂移。

```bash
python3 - <<'PY'
import re
def normalize(s):
    s = re.sub(r'%.*', '', s)
    s = re.sub(r'\\label\{[^}]*\}', '', s)
    s = re.sub(r'\\(?:ref|eqref|cref|Cref|cite[a-zA-Z]*)\{[^}]*\}', '', s)
    s = re.sub(r'\\(?:emph|textbf|textit|mathrm|mathbf|mathsf|mathcal|operatorname)\{([^{}]*)\}', r'\1', s)
    s = re.sub(r'\\begin\{[^}]+\}|\\end\{[^}]+\}', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()
# 比较当前主体文件中的标准化定理块
# 反对他们的附录重述。任何不匹配都会阻止完成。
PY
```

**经验动机：**在一次真实的提交运行中，一个关键定理在正文中出现了多例分割，但在附录中出现了单例陈述；关键变量在主文件中以一种方式命名，在附录中以另一种方式命名。这些在修复轮次中多次漂移，因为没有自动检查捕获回归。

**可选更深层次检查：`/proof-checker --restatement-check`**

上面的内联 Python 检查是默认的，足以保证例程 main-vs-appendix 的一致性。为了获得更广泛的覆盖范围，您还可以调用 `/proof-checker --restatement-check`（在 PR #189 中添加），这将比较扩展到：

- 摘要表、摘要、“关键贡献”列表和讨论部分（不仅仅是主要部分和附录）中的重述。
- 审稿人分类的6个命名漂移签名为：`conditional_loss`、`scope_change`、`quantifier_loss`、`regime_envelope_change`、`constant_change`、`variable_rename`。
- 在 `PROOF_AUDIT.json` 中以 `details.restatement_drift[]` 形式发出的结构化结果，适用于下游工具或审计跟踪。

这仅是建议性的 - 内联步骤 4.5 检查仍然是默认设置，并在每个循环中继续运行。当（a）您怀疑主↔附录轴之外的跨位置漂移（例如，相对于定理陈述的抽象过度主张），或（b）您想要审稿人分级的漂移签名而不是原始字符串不匹配时，请考虑调用`/proof-checker --restatement-check`。支持同时运行两者，并且它们是独立的：内联检查在字符串漂移上快速失败，证明检查器传递表面语义类漂移。

### 第五步：第二轮审稿

如果 `REVIEWER_BIAS_GUARD = true`（默认），请使用 **新** `mcp__codex__codex` 线程进行第 2 轮。请勿重复使用第 1 轮线程 ID 进行提示。保存返回的threadId仅用于恢复记账。

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    您正在审阅一篇[目标会议或期刊]论文。这是一份新鲜的、零上下的审稿。

    判断是否断定两个方向上的校准。建议仅在以下情况下缩小范围：
    当前的范围或方式超出了证据范围；不要要求额外的对冲
    围绕支持的结果。旗帜堆叠树篱，自卫（“我们不
    论断”）、指示声明（“我们不涉及 X”）以及一般警告
    外部限制作为需要删除的书写缺陷。音调修正绝不能改变
    事实、否定、形式、范围、比较方向或数字。
    还标记叙述缺陷：进度报告结构（“我们首先尝试
    A，然后 B")，一个建立在方法丢失的度量基础上的故事，叙述结果
    作为失败（“表现不佳”、“未能超越”）而不是解释为
    目标差异或权衡、没有争论义务的实验、
    摘要或介绍以背景或实施为开端
    而不是问题 -> 差距 -> 想法 -> 最强结果和结论
    最终以新的自我否定结束。解决办法是重新构建并裁剪
    证据支持重构；陈述了真正的弱点
    中立并保持在限制中。切勿删除其中不利的号码
    桌子，并且永远不要将弱点作为权衡。
    忽略任何先前的审稿轮次、先前的修复列表或执行者的解释。
    仅从当前的 LaTeX 源码和编译的 PDF 来判断该论文。

    ## 论文文件：
    - LaTeX 来源：[列出所有节 .tex 文件]
    - 编译 PDF : paper/main.pdf
    - 图：[列表图文件]

    阅读 LaTeX 源代码（对于 content/logic ）和编译后的 PDF （对于视觉传输方式）。

    ## 审稿须知
    请担任高级ML审稿人（[目标会议或期刊]级别）。提供：
    1. **总分**（1-10，其中6 = 弱接受，7 = 接受）
    2. **总结**（2-3句话）
    3. **优势**（项目符号列表，排名）
    4. **弱点**（项目符号列表，排名：关键>主要>次要）
    5. **针对每个 CRITICAL/MAJOR 弱点**：具体的、可操作的修复
    6. **缺少参考文献**（如果有）
    7. **视觉审稿**（来自PDF）：
       - 图形质量：可读？标签清晰易读吗？灰度中可以区分颜色吗？
       - 图标题对齐：每个标题是否与其图匹配？
       - 布局：孤立的标题、尴尬的分页符、远离参考文献的数字？
       - 表格格式：对齐的列、一致的小数、粗体以获得最佳结果？
       - 视觉一致性：所有图形都使用相同的配色方案？
    8. **结论**：准备好提交了吗？是/几乎/否

    重点：理论严谨性、论断与证据的一致性、写作清晰度、
    自包含性、符号一致性和视觉呈现方式质量。
```

如果是 `REVIEWER_BIAS_GUARD = false`（仅限旧式调试），则将 `mcp__codex__codex-reply` 与保存的 threadId 一起使用；这**不是**推荐的路径。

### 步骤5.5：消除论证练习（仅限理论/范围重的论文）

仅当论文是理论重的（源中≥5个`\begin{theorem}|\begin{lemma}|\begin{proposition}|\begin{corollary}`环境）或在title/abstract中有明确的scope/generality论断时，并且仅在最后预定的轮次（`current_round == MAX_ROUNDS`）上运行。

**委托给 `/kill-argument`** — 规范实现位于 `skills/kill-argument/SKILL.md`（于 2026 年 5 月提取）。此步骤不会重新实现攻击和裁决提示模板；相反，调用该技能并读取其输出：

```bash
# 援引当前论文的规范对抗性审稿技术基元。
# /kill-argument 运行两个新线程 codex gpt-6-astra ultra 调用和写入
# KILL_ARGUMENT .{md,json} 进入论文目录。它仅用于检测 -
# 它从不编辑论文本身。
/kill-argument "$PAPER_DIR"

# 阅读结构化判决。
KILL_VERDICT=$(jq -r '.verdict' "$PAPER_DIR/KILL_ARGUMENT.json")
KILL_REASON=$(jq -r '.reason_code' "$PAPER_DIR/KILL_ARGUMENT.json")
```

**合并规则**（自动循环的责任 - `/kill-argument` 本身仅检测）：

- 从 `KILL_ARGUMENT.json` 读取 `details.decomposed_points` 。
- 对于 `severity_if_unresolved == "critical"` 处具有 `verdict == "still_unresolved"` 或 `verdict == "partially_answered"` 的每个点：
  - 通过语义重叠（约 85% 相似度阈值）对第 2 轮弱点列表进行重复数据删除。
  - 如果新颖，请将其附加到第 6 步修复列表，并使用推荐修复作为操作描述。
- 如果裁决显示问题是 `answered_by_current_text` ，请在亲自验证引用的 file:line 证据后仅降级现有的弱点项目。
- 将 `KILL_ARGUMENT.md` 和合并决策记录在 `PAPER_IMPROVEMENT_LOG.md` 中。
- 如果是 `HUMAN_CHECKPOINT = true` ，请在要求用户继续之前将合并的结果包含在检查点摘要中。

此阶段直接进入步骤 6。合并的结果必须在最终重新编译之前落地。

如果 `/kill-argument` 返回 `verdict: NOT_APPLICABLE` （论文的理论或范围不足以需要此检查），则完全跳过步骤 5.5 并继续执行步骤 6。如果它返回 `BLOCKED` 或 `ERROR` ，请将原因记录在 `PAPER_IMPROVEMENT_LOG.md` 中并继续而不进行合并 - 循环不应在无法进行的对抗性检查上停止。运行。

**经验动机：**在一次真正的提交运行中，经过几轮标准改进（得分 7-8/10 ），杀戮论证练习暴露了之前审稿没有发现的框架弱点（例如，设置主要是有条件的而不是真正通用的，或者基线与真实系统无关）。作者反驳在摘要和讨论中强制明确范围限定。

### 步骤 5b：人工检查点（如果启用）

**如果是 `HUMAN_CHECKPOINT = false` 则跳过。** 与步骤 2b 相同 — 呈现第 2 轮审稿，等待用户输入。

### 第 6 步：实施第 2 轮修复

与步骤 3 相同的过程。典型的第 2 轮修复：
- 添加验证理论的受控合成实验
- 重新检查两个方向的校准：缩小真正的夸大主张，直接声明支持论断，将分散的一般警告合并为限制——并且不要重新软化已经支持的论断证据
- 将非正式论证形式化（例如，截断→正式命题）
- 仅当缺少限制时才使限制更加具体

**编辑白名单门（如果设置）：** 与步骤 3 相同 - 如果设置了 `EDIT_WHITELIST`，则在应用每个建议的编辑之前运行路径 + 禁止操作检查。拒绝记录到 `## Rejected by edit_whitelist (Round 2)` 下的 `PAPER_IMPROVEMENT_LOG.md` 中，并且循环继续。在回合结束时显示拒绝摘要。

### 第 7 步：重新编译第 2 轮

```bash
cd paper && latexmk -C && latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
cp main.pdf main_round2.pdf
```

### 第8步：格式检查

最终重新编译后，运行 **位置感知** 格式合规性检查。

```bash
# 如果日志缺少 file/line 数据，请使用 -file-line-error 重新运行一次最终编译。
cd paper && latexmk -pdf -file-line-error -interaction=nonstopmode -halt-on-error main.tex
```

```bash
# 1. 页数与目标会议或期刊限制
PAGES=$(pdfinfo paper/main.pdf | grep Pages | awk '{print $2}')
echo "Pages: $PAGES (limit: 9 main body for ICLR/NeurIPS)"

# 2. 重复标签：HARD BLOCK
DUP_LABELS=$(grep -Rho "\\\\label{[^}]*}" paper/main.tex paper/sections 2>/dev/null | sort | uniq -d || true)
if [ -n "$DUP_LABELS" ]; then
    echo "Duplicate labels found (BLOCKING):"
    echo "$DUP_LABELS"
fi

# 3. 超满警告及位置分类
OVERFULLS=$(grep -n "Overfull \\\\hbox" paper/main.log 2>/dev/null || true)

# 主体 = main.tex 中 \appendix 之前的源文件。
# 附录 = \appendix 之后的源文件，或路径包含“appendix”的文件。
# 参考书目 = 论文.bbl、references.bib 或参考书目生成的输出。
MAIN_BODY_OVERFULL=$(echo "$OVERFULLS" | grep -v -E 'appendix|paper\.bbl|references\.bib' || true)
APPENDIX_OVERFULL=$(echo "$OVERFULLS" | grep -E 'appendix' || true)
BIB_OVERFULL=$(echo "$OVERFULLS" | grep -E 'paper\.bbl|references\.bib' || true)

echo "Main-body overfulls (any size BLOCKS):"
echo "$MAIN_BODY_OVERFULL"
echo "Appendix overfulls (>10pt blocks):"
echo "$APPENDIX_OVERFULL"
echo "Bibliography overfulls (>20pt blocks):"
echo "$BIB_OVERFULL"
```

**停止标准：**
- 任何重复的标签都会阻止完成。
- 无论大小如何，主体中的任何溢出都会阻止完成。
- 仅当附录超过 10pt 或明显被剪裁时，附录才会溢出块完成。
- 仅当参考书目超过 20pt 或明显被剪裁时，才会超出块完成状态。
- 未满的水平盒仍会发出警告，除非它们造成明显的布局损坏。

**自动修复模式（位置感知）：**

| 问题 | 使固定 |
|-------|-----|
| 方程主体溢出 | 用 `aligned` / `split` / `multline` 拆分，或缩短符号 |
| 表中主体已满 | 减少字体、调整表格大小或跨行打破表格 |
| 正文正文过满 | 改写；不要用全局 `\sloppy` 隐藏它 |
| 附录过满≤10pt | 仅发出警告，除非出现明显削波 |
| 附录过满 > 10pt | 如果溢出可见，请应用相同的修复程序 |
| 参考书目超满≤20pt | 仅发出警告，除非由格式错误的条目或剪辑引起 |
| 参考书目已满 > 20pt | 修复格式错误的条目、URL 或 DOI 格式 |
| 超出页数限制 | 将内容移至附录、压缩表格、缩小图形尺寸 |

**位置感知解释：**
- 按`-file-line-error`日志中报告的源文件进行分类。
- 如果警告无法分类，则将其视为主体并修复。

**经验动机：** 在一次真实的提交运行中，数十个过满的 hbox 警告（最大的超过 100pt 的附录证明）在多次改进中幸存下来，因为之前的“overfull > 10pt 块”规则过于宽松，并且平等对待所有位置。

### 第 9 步：记录结果

在论文目录下创建`PAPER_IMPROVEMENT_LOG.md`：

```markdown
# 论文改进日志

## 分数进展

| 圆形的 | 分数 | 判决 | 关键修改 |
|-------|-------|---------|-------------|
| 第 0 轮（原始） | X/10 | No/Almost/Yes | 基线 |
| 第一轮 | Y/10 | No/Almost/Yes | [修复摘要] |
| 第二轮 | Z/10 | No/Almost/Yes | [修复摘要] |

## 第一轮审稿及修复

<详情>
<摘要> GPT-6-Astra xhigh审稿（第1轮）</summary>

[完整原始审稿文本，逐字记录]

</details>

### 已实施的修复
1. [修正说明]
2. [修正说明]
...

## 第二轮审稿及修复

<详情>
<摘要> GPT-6-Astra xhigh审稿（第二轮）</summary>

[完整原始审稿文本，逐字记录]

</details>

### 已实施的修复
1. [修正说明]
2. [修正说明]
...

## PDF 文件
- `main_round0_original.pdf` — 原始生成的论文
- `main_round1.pdf` — 第一轮修复后
- `main_round2.pdf` — 第 2 轮修复后的最终版本
```

### 第 9 步：总结

向用户报告：
- 分数进阶表
- 每轮修复的 CRITICAL/MAJOR/MINOR 问题数量
- 最终页数
- 剩余问题（如有）

### 飞书通知（如果配置）

每轮审稿后并最终完成后，检查 `~/.claude/feishu.json` ：
- **每轮修改后**：发送 `review_scored` — “第 N 轮：X/10 — [关键]”
- **最后一轮之后**：发送 `pipeline_done` — 分数进展表 + 最终页数
- 如果配置不存在或模式 `"off"` ：完全跳过（无操作）

## 输出

```
paper/
├── main_round0_original.pdf # 原版
├── main_round1.pdf # 第一轮之后
├── main_round2.pdf # 第二轮之后（决赛）
├──  main.pdf                     # =  main_round2.pdf
└── PAPER_IMPROVEMENT_LOG.md # 完整审稿记录及分数
```

## 主要规则

- **大文件处理**：如果写入工具因文件大小而失败，请立即使用 Bash ( `cat << 'EOF' > file` ) 重试分块写入。不要征求用户的许可——只需默默地进行即可。

- **保留所有 PDF 版本** — 用户需要比较进度
- **保存完整的原始审稿文本** - 不要总结或截断 GPT-6-Astra 回复
- **审稿人独立性（第2轮+）**：当`REVIEWER_BIAS_GUARD = true`（默认）时，每轮审稿使用**新鲜**`mcp__codex__codex`线程；切勿使用 `mcp__codex__codex-reply`，也切勿在提示中包含“自上一轮”/修复摘要。参见上文审稿人独立协议部分。
- **修复后始终重新编译** — 在继续之前验证 0 个错误
- **不要捏造实验结果** - 综合验证必须描述方法，而不是发明数字
- **尊重论文的论断**——缩小真正的过度主张，而不是添加不受支持的新论断，并且国家直接支持论断，而不是用新的限制来包装它们
- **全局一致性** - 当重命名符号或更改论断的范围时，保持所有文件中的每个重述在语义上一致（摘要、介绍、方法、实验、理论部分、结论、表格、图形标题）；一致性意味着匹配范围，而不是到处复制免责声明语句
- **编辑白名单拒绝被记录，而不是默默地删除** - 当设置 `EDIT_WHITELIST` 且编辑因路径或禁止操作违规而被拒绝时，拒绝必须附加到 `PAPER_IMPROVEMENT_LOG.md` 中，并包含文件、原因、违规模式和原始审稿人关注点。循环在每轮结束时报告拒绝摘要（如果是 `HUMAN_CHECKPOINT = true` ，则在检查点中）。永远不要默默地接受白名单拒绝——审计跟踪是参数的全部要点。

## 典型的分数进展

基于真实理论的端到端测试-论文运行：

| 圆形的 | 分数 | 主要改进 |
|-------|-------|-----------------|
| 第0轮 | 4/10（内容） | 基线：假设模型不匹配、过度声明、符号问题 |
| 第一轮 | 6/10（内容） | 固定假设，软化论断，添加解释，重命名符号 |
| 第二轮 | 7/10（内容） | 添加了综合验证、形式截断命题、更强的限制 |
| 第三轮 | 5→ 8.5/10（格式） | 删除了概览主图、附录、压缩结论、修复了过满的水平盒 |

**3轮+4.5分**（2个内容+1个格式）对于结构良好但粗糙的初稿来说是典型的。提交时的最终状态：清除 overfull-hbox 计数和目标会议或期刊格式兼容的长度。

## 审稿溯源

每次`mcp__codex__codex`或`mcp__codex__codex-reply`审稿人调用后，保存以下`shared-references/review-tracing.md`的痕迹（策略C——取证；永远不要默默地跳过）。使用 `save_trace.sh` （根据 `shared-references/integration-contract.md` §2 中的链进行解析）或直接将文件写入 `.aris/traces/<skill>/<date>_run<NN>/` 。遵守 `--- trace:` 参数（默认值： `full` ）。

