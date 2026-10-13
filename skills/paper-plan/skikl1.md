---
name: paper-plan
description: "根据审稿结论和实验结果生成结构化的论文大纲。当用户说“写大纲”、“论文大纲”、“计划论文”、“论文规划”或想要在写作之前创建论文计划时使用。"
argument-hint: "[topic-or-narrative-doc] [— style-ref: <source>]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, Skill, mcp__codex__codex, mcp__codex__codex-reply
---

# 论文计划：从审稿结论到论文大纲

从以下位置生成结构化的逐节论文大纲：**$ARGUMENTS**

## 常量

- **REVIEWER_MODEL = `gpt-6-astra`** — 通过 Codex MCP 审稿大纲使用的模型。必须是 OpenAI 型号。
- **TARGET_VENUE = `ICLR`** — 默认目标会议或期刊。用户可以覆盖（例如， `/paper-plan "topic" — venue: NeurIPS` ）。支持：`ICLR`、`NeurIPS`、`ICML`、`CVPR`、`ACL`、`AAAI`、`ACM`、`IEEE_JOURNAL`（IEEE Transactions / Letters）、`IEEE_CONF` （IEEE 会议）。
- **MAX_PAGES** — 页数限制。对于机器学习会议：主体到结论结束（不包括参考文献、附录）。 ICLR=9、NeurIPS=9、ICML=8、AAAI=7 技术内容页面加参考文献，除非当前 AAAI CFP 另有说明。 **对于 IEEE 会议或期刊：参考文献包含在页数中。** IEEE 期刊交易 ≈ 总共 12-14 页，信件 ≈ 总共 4-5 页； IEEE 会议约 5-8 页（包括参考文献）。

## 输入

该技能需要项目目录中包含以下一项或多项：

1. **NARRATIVE_REPORT.md** 或 **STORY.md** — 带论断和证据的研究叙述
2. **review-stage/AUTO_REVIEW.md** —自动审稿循环结论*（如果没有找到则回退到`./AUTO_REVIEW.md`）*
3. **实验结果** — `figures/` 、screen 日志、表格中的 JSON 文件
4. **idea-stage/IDEA_REPORT.md** — 来自创意发现流程（如果适用）*（如果未找到，则返回到 `./IDEA_REPORT.md`）*
5. **紧凑文件**（如果可用）：`idea-stage/IDEA_CANDIDATES.md` *（如果找不到，则回退到 `./IDEA_CANDIDATES.md`）*、`findings.md`、`EXPERIMENT_LOG.md` — 优先于完整文件（如果存在），保存上下文窗口

如果不存在，请用户用 3-5 句话描述论文的贡献。

## Orchestra - 引导书写覆盖

保留现有的 `insleep` 工作流程和输出，但使用下面的共享参考来提高故事和大纲的质量。

- 在构建一句话贡献、摘要、简介、相关工作或英雄人物时，请阅读 `../shared-references/writing-principles.md`。
- 在冻结特定目标会议或期刊的大纲之前，请先阅读 `../shared-references/venue-checklists.md`。
- 仅在需要时加载这些参考文献；不要将其全部内容粘贴到工作草案中。

## 可选：样式参考（ `— style-ref: <source>` ，选择加入）

让用户引导大纲的**结构**布局（章节排序、小节密度、定理环境密度、图形预算、引文风格）走向参考论文。 **默认关闭——当用户没有通过 `— style-ref` 时，与之前不做任何不同的事情。**

仅当 `— style-ref: <source>` 出现在 `$ARGUMENTS` 中时，才先运行辅助程序，然后再起草大纲：

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
  0) ;;                                       # 使用$CACHE / style_profile.md作为结构指导
  2) echo "warning: style-ref skipped (missing optional dep)" >&2 ;;
  3) echo "error: --style-ref source failed; aborting outline" >&2 ; exit 1 ;;
  *) echo "error: helper failed unexpectedly; aborting outline" >&2 ; exit 1 ;;
esac
```

接受的来源：本地 TeX 目录/文件、本地 PDF 、 arXiv id （ `2501.12345` 或 `arxiv:2501.12345` ）、http(s) URL。  Overleaf URL 和项目 ID 被拒绝 - 首先通过 `/overleaf-sync setup <id>` 克隆并传递本地克隆路径。

**严格的规则**（`tools/extract_paper_style.py` 文档字符串中的完整合同）：

- 仅在提出大纲的章节列表、小节计数、定理密度、图形预算时，才使用 `style_profile.md` 作为**结构**指导。
- **切勿从可通过缓存访问的任何内容中逐字复制散文、论断、示例、章节名称或术语**。用户的叙述是唯一的实质来源。
- **切勿将`— style-ref`（或缓存内容）传递给审稿人/审计分代理。**跨模型审稿独立性（`../shared-references/reviewer-independence.md`）要求审稿人只能看到产品和用户的提示。

### 差距报告（ `GAP_REPORT.md` ，当 style-ref 打开时自动发出）

当 `— style-ref:` 成功并且项目中存在 `figures/` 、 `results/` 、 `data/` 、 `tables/` 、 `sec/` 、 `NARRATIVE_REPORT.md` 、 `CLAIMS_FROM_RESULTS.md` 中的任何一个时，**也会**在起草大纲之前发出差距报告。差距报告将示例的截面拓扑+密度要求（来自 `style_profile.md`）与用户的实际资产进行映射，在用户**没有证据可以填充**的地方显示结构槽。这是 `/paper-write` 决定何时发出 `<!-- DATA_NEEDED -->` 标记而不是伪造内容的合同。

程序：

1. 阅读 `$CACHE/style_profile.md` 以获取示例的部分列表 + 每个部分的特征计数（每个部分的图形、定理、表格、引文、句子）。
2. 盘点用户资产：`figures/*` 文件名、`results/*` 证据文件、`sec/*.tex` 现有散文、`NARRATIVE_REPORT.md`、`CLAIMS_FROM_RESULTS.md`（如果 `/result-to-claim` 运行）、`references.bib` 引用密度。
3. 对于示例所暗示的每个部分槽（消融实验表、缩放实验、故障案例分析、证明块等），分类为 `covered` / `partial` / `missing` 。
4. 发出 `<output-dir>/GAP_REPORT.md` ：

```markdown
# GAP_REPORT — 示例与用户资产

- **示例源：** <源标识符（文件路径、arXiv ID、URL）>
- **生成：** <UTC ISO-8601>
- **样式配置文件：** <style_profile.md 的相对路径>

## 截面拓扑间隙

| 示例槽 | 典型特征 | 用户证据 | 地位 | 槽位号 |
|---|---|---|---|---|
| §5 实验 | 消融实验台（3轴×4层） | `results/`没有消融实验文件 | 丢失的 | `GAP_S5_ABLATION` |
| §5.3 缩放 | log-N 缩放曲线 | `figures/scaling.pdf` 未找到 | 丢失的 | `GAP_S5_SCALING` |
| §6 讨论 | 失败案例分析 | 不存在于 `NARRATIVE_REPORT.md` 中 | 丢失的 | `GAP_S6_FAILURE` |
| §2 相关 | 引用密度≥60 | `references.bib` 有 35 条记录 | 部分的 | `GAP_S2_CITES` |

## 覆盖范围摘要

- 覆盖：N
- 部分：M
- 缺失：K

## 使用者

- `/paper-write` 读取此文件并为 `missing` 插槽发出 `<!-- DATA_NEEDED: <Slot ID> — <one-line description> -->` 占位符，而不是伪造内容。
- `/paper-claim-audit` 可以使用 Slot ID 来标记引用具有 `missing` 证据的部分的论断。
```

插槽 ID 格式： `GAP_<SECTION>_<FEATURE>` ，全部大写，在再生过程中保持稳定，除非用户资产发生变化。

**规则**（硬）：

- **不要**通过推断、填补或幻觉证据来“弥补”差距。失踪了就是失踪了。
- **不要**提出具体的实验命令来填补空白——这是 `/experiment-bridge` 的工作。差距报告只是表面上的赤字。
- **不要**包含范例散文/论断文本/作者姓名/范例中的定量数字。
- 如果 `style_profile.md` 提取失败或用户没有项目资产，请跳过差距报告（没有错误；只是不发出文件）。
- 差距报告**也受到审稿人隔离**——从未传递给审稿人/审核员分代理人（与`style_profile.md`相同的规则）。

原始想法：@zhangpelf in [#217](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/issues/217)。

## 工作流程

### 步骤一：提取论断和证据

**首先检查 `CLAIMS_FROM_RESULTS.md`** - 如果其第一行是 `verdict: REVIEW_UNAVAILABLE` ，则将该文件视为 ABSENT 进行论断提取（进入下面的叙述性文档），然后：在 `— assurance: submission` 下（ `shared-references/assurance-contract.md` ；由 `— effort: max|beast` 暗示） STOP - 论断从未被裁定，重新运行`/result-to-claim`第一；在 `assurance: draft` 下继续，但在论断矩阵中标记每个论断 `[unadjudicated]`。否则，如果存在（由`/result-to-claim`在工作流程2结束时生成），则将其用作论断的起点。该文件包含已经映射到实验证据的经过验证的论断。与以下叙述性文件中的任何其他论断合并。

如果`CLAIMS_FROM_RESULTS.md`不存在，则从头开始提取论断：

阅读所有可用的叙述性文件并摘录：

1. **核心论断** (3-5 主要贡献)
2. **一句话贡献**（最能说明论文贡献的一句话）
3. **每个论断的证据**（哪些实验、哪些指标、哪些数字）
4. **已知的弱点**（来自审稿人反馈）
5. **建议框架**（来自审稿结论）

建立一个**论断-证据矩阵**：

```markdown
| 论断 | 证据 | 地位 | 部分 |
|-------|----------|--------|---------|
| [论断 1] | [表达式 A，度量 B] | 支持 | §3.2 |
| [论断 2] | [实验C] | 部分支持 | §4.1 |
```

### 第二步：确定论文类型和结构

根据TARGET_VENUE和论文内容，进行分类和结构选择。

在确定结构之前，请应用 `../shared-references/writing-principles.md` 中的叙述原则：

- 这篇论文应该讲述一个连贯的技术故事。
- 在引言结束时，大纲应明确**什么**、**为什么**和**所以什么**。
- 预先加载最重要的内容：标题、摘要、简介和英雄人物。 审稿人往往在阅读全文之前就形成判断。

**重要**：部分计数灵活（5-8 个部分）。选择最适合内容的内容。下面的模板是起点，而不是严格的约束。

**Empirical/Diagnostic  论文:**
```
1. 简介（1.5 页）
2. 相关工作（1 页）
3. 方法/设置（1.5 页）
4. 实验（3页）
5. 分析/讨论（1 页）
6. 结论（0.5 页）
```

**理论+实验论文：**
```
1. 简介（1.5 页）
2. 相关工作（1 页）
3. 预备知识和建模（1.5 页）
4. 实验（1.5页）
5. 理论 A 部分（1.5 页）
6. 理论 B 部分（1.5 页）
7. 结论（0.5 页）
— 总计：9 页
```
理论论文通常需要7个部分（将理论分为估计+优化，或设置+分析）。总页面预算必须总计为 MAX_PAGES 。

理论论文应该：
- 包括**概要证明**位置（不仅仅是定理陈述）
- 规划一个先前理论界限与本文界限的**比较表**
- 确定附录和正文中的证明

**论文方法：**
```
1. 简介（1.5 页）
2. 相关工作（1 页）
3. 方法（2页）
4. 实验（2.5 页）
5. 消融实验/分析 (1 页)
6. 结论（0.5 页）
```

### 第三步：分段规划

对于每个部分，请指定：

```markdown
### §0 摘要
- **我们实现了什么**：[论文的具体贡献，而不是领域级别的背景]
- **为什么它很重要/很困难**：[为什么这个问题很重要并且不平凡]
- **我们是如何做的**：[一句话方法]
- **证据**：[什么支持论断]
- **最显着的结果**：[最强的定量或理论结果]
- **预计长度**：150-250 个字
- **独立检查**：读者可以在没有论文的情况下理解这一点吗？

### §1 简介
- **开头钩子**：[激发问题的 1-2 句话]
- **差距/挑战**：[之前的工作中缺少什么，以及为什么之前的工作不足]
- **一句话贡献**：[论文的主要内容]
- **方法概述**：[我们的不同做法]
- **关键问题**：[本文回答的研究问题]
- **贡献**：[2-4个编号项目符号，具体且可证伪，匹配论断-证据矩阵]
- **结果预览**：[最强的结果或与早期表面的比较]
- **英雄人物**：[描述图 1 应显示的内容 — 必须包括清晰的比较（如果适用）]
- **预计长度**：1.5 页
- **关键引用**：[此处引用 3-5 篇论文]
- **前端加载检查**：[略读的读者在到达方法之前会知道主要的论断吗？]

### §2 相关工作
- **副主题**：[2-4类相关工作]
- **定位**：[这篇论文与每个类别有何不同]
- **最小长度**：1整页（至少3-4段落并进行实质性综合）
- **组织规则**：按方法论族/假设/问题组织，而不是逐篇论文组织
- **不能只是一个列表**——综合、比较和定位

### §3 方法/设置/预备
- **符号**：[关键符号及其含义]
- **问题表述**：[正式设置]
- **方法描述**：[算法、模型或实验设计]
- **正式陈述**：[定理、命题（如果适用）]
- **说明概要位置**：[此处显示的关键步骤与附录]
- **预计长度**：1.5-2 页

### §4 实验/主要结果
- **计划数字**：
  - 图1：[描述，类型：bar/line/table/architecture，它显示的比较]
  - 图2：【说明】
  - 表1：【它显示了什么，与methods/baselines进行比较】
- **数据来源**：【其中JSON文件/实验结果】

### §5 结论
- **重述**：[贡献重新表述，而不是从简介中复制粘贴]
- **局限性**：[诚实的评估——审稿人看重这一点]
- **未来的工作**：[1-2具体方向]
- **预计长度**：0.5 页
```

### 第四步：图形计划

列出每个图和表：

```markdown
## 图计划

| ID | 类型 | 描述 | 数据来源 | 优先事项 |
|----|------|-------------|-------------|----------|
| 图1 | Hero/Architecture | 系统概述+比较 | 手动的 | 高的 |
| 图2 | 线图 | 训练曲线比较 | figures/exp_A.json | 高的 |
| 图3 | 条形图 | 消融实验结果 | figures/ablation.json | 中等的 |
| 表1 | 对照表 | 主要结果与基线 | figures/main_results.json | 高的 |
| 表2 | 理论比较 | 先前的界限与我们的界限 | 手动的 | 高（理论论文） |
```

**图 1/英雄人物的关键**：详细描述该人物应包含的内容，包括：
- 正在比较哪些方法
- 视觉差异应展示什么
- 清楚说明比较的标题草稿
- 为什么该图可以帮助浏览者在阅读完整方法之前理解论文

### 第 5 步：引文脚手架

对于每个部分，列出所需的引用：

```markdown
## 引文计划
- §1 简介：[paper1]、[paper2]、[paper3]（问题动机）
- §2 相关：[paper4]-[paper10]（按副主题分类）
- §3 方法：[paper11]（基线），[paper12]（我们构建的技术）
```

**引用规则**（来自claude-scholar + Imbad0202/academic-research-skills）：
1. 永远不要从内存中生成 BibTeX — 始终通过搜索或现有 .bib 文件进行验证
2. 每个引用都必须经过验证：正确的作者、年份、目标会议或期刊
3. 使用 `[VERIFY]` 标记您不确定的任何引文
4. 在可用时，优先选择已发布的版本而不是 arXiv 预印本

### 第六步：与REVIEWER_MODEL交叉审稿

将完整的大纲发送至 GPT-6-Astra xhigh 以获取反馈：

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    审稿本论文大纲，供 [目标会议或期刊] 提交。
    [完整大纲，包括论断-证据矩阵]

    得分 1-10 分：
    1. 逻辑流程——故事的展开是否自然？
    2. 论断-证据一致性——每个论断都得到支持吗？
    3. 缺少实验或分析
    4. 相对于之前工作的定位
    5. 页面预算可行性（ MAX_PAGES = 正文到结论结束，不包括 refs/appendix ）
    6. 前置强度——摘要、引言、主图方案是否足够强以供略读审稿人？

    对于每个弱点，建议最小修复。
    具体且可操作——“添加X”而不是“考虑更多实验”。
```

在最终确定之前应用反馈。

### 第7步：输出

将最终大纲保存到项目根目录中的 `PAPER_PLAN.md` 中：

```markdown
# 论文计划

**标题**：[工作标题]
**一句话贡献**：[论文核心要点的一句话陈述]
**目标会议或期刊**: [目标目标会议或期刊]
**类型**：[ empirical/theory/method ]
**日期**：[今天]
**页面预算**：[ MAX_PAGES ] 页数（正文到结论结束，不包括参考文献和附录）
**节数**：[N]（必须与将创建的节文件数匹配）

## 论断-证据矩阵
[来自步骤 1]

## 结构
[从步骤 2-3 开始，逐节]

## 图计划
[从第4步开始，有详细的英雄人物描述]

## 引文计划
[来自第 5 步]

## 审稿人反馈
[总结第 6 步]

## 下一步
- [ ] /paper-figure 生成所有数字
- [ ] /paper-write 起草 LaTeX
- [ ] /paper-compile 构建 PDF
```

## 主要规则
- **大文件处理**：如果写入工具因文件大小而失败，请立即使用 Bash ( `cat << 'EOF' > file` ) 重试分块写入。不要征求用户的许可——只需默默地进行即可。
- **不生成作者信息** - 将作者块保留为占位符或匿名
- **诚实对待证据差距** — 将论断标记为“需要实验”而不是夸大其词
- **页面预算很困难** - 如果内容超过 MAX_PAGES ，建议将哪些内容移至附录
- **MAX_PAGES 计数因目标会议或期刊而异** — ML 会议：主体到结论结束，references/appendix 不计数； AAAI 主轨道通常由 7 个技术内容页面和参考文献组成。 **IEEE 会议或期刊：参考文献计入页数限制。**
- **目标会议或期刊特定规范** - ML会议（ICLR/NeurIPS/ICML）使用`natbib`（`\citep` / `\citet`）； **IEEE 会议或期刊使用 `cite` 包（ `\cite{}` ，数字样式）**
- **论断-证据矩阵是支柱** - 每个论断都必须映射到证据，每个实验都必须支持一个论断
- **预先加载故事** - 在读者到达完整方法之前，大纲应在标题、摘要、介绍和概览主图中明确贡献
- **人物需要详细描述**——尤其是英雄人物，必须清楚地说明比较和视觉期望
- **章节数量灵活** — 5-8 个章节，具体取决于论文类型。不要将内容强行放入严格的 5 部分模板中。

## 致谢

受[Research-paper-Writing-Skills](https://github.com/Master-cai/Research-Paper-Writing-Skills)（论断证据映射）、[claude-scholar](https://github.com/Galaxy-Dawn/claude-scholar)（引文验证）和[Imbad0202/academic-research-skills](https://github.com/Imbad0202/academic-research-skills)（论断验证协议）启发的概述方法。该混合包中的写作框架覆盖改编自 Orchestra Research 的论文写作指南。

## 输出协议

> 所有输出文件均遵循以下共享协议：
> - **[输出版本控制协议](../shared-references/output-versioning.md)** — 首先写入带时间戳的文件，然后复制到固定名称
> - **[输出清单协议](../shared-references/output-manifest.md)** — 将每个输出记录到 MANIFEST.md
> - **[输出语言协议](../shared-references/output-language.md)** — 尊重项目的语言设置

