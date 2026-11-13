---
name: paper-write
description: "从大纲中逐节起草 LaTeX 论文。当用户说“写论文”、“写论文”、“草稿 LaTeX”、“开始写”或想要从论文计划生成 LaTeX 内容时使用。"
argument-hint: "[venue-or-section] [— style-ref: <source>]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, mcp__codex__codex, mcp__codex__codex-reply
---

# 论文写作：逐节 LaTeX 生成

起草 LaTeX 论文基于：**$ARGUMENTS**

## 常量

- **REVIEWER_MODEL = `gpt-6-astra`** — 通过 Codex MCP 审稿部分使用的模型。必须是 OpenAI 型号。
- **TARGET_VENUE = `ICLR`** — 默认目标会议或期刊。支持：`ICLR`、`NeurIPS`、`ICML`、`CVPR`（还有ICCV/ECCV）、`ACL`（还有EMNLP/NAACL）、`AAAI`、`ACM`（ACM MM、 SIGIR、KDD、CHI 等）、`IEEE_JOURNAL`（IEEE Transactions / Letters，例如 T-PAMI、JSAC、TWC、TCOM、TSP、TIP）、`IEEE_CONF`（IEEE 会议，例如 ICC、GLOBECOM、INFOCOM、ICASSP）。确定样式文件和格式。
- **ANONYMOUS = true** — 如果为 true，则使用匿名作者块。将 `false` 设置为最终提交版本。注意：大多数 IEEE 会议或期刊不使用匿名提交 — 为 IEEE 设置 `false`。
- **MAX_PAGES = 9** — 主体页数限制。对于机器学习会议：从首页到结论部分结束的计数，参考文献和附录不计算在内。 **对于 IEEE 会议或期刊：参考文献计入页数限制。** 典型限制：IEEE 期刊 = 无严格限制（但交易通常为 12-14 页，信件为 4-5 页），IEEE 会议 = 5-8 页，包括参考文献。
- **DBLP_BIBTEX = true** — 从 DBLP/CrossRef 获取真实的 BibTeX，而不是 LLM 生成的条目。消除幻觉引用。需要零安装。将 `false` 设置为使用旧行为（LLM 搜索 + `[VERIFY]` 标记）。

## 输入

1. **PAPER_PLAN.md** — 带论断证据矩阵的轮廓、剖面图、图形图（来自 `/paper-plan` ）
2. **NARRATIVE_REPORT.md** — 研究叙述（内容的主要来源）
3. **生成的数字** - `figures/` 中的 PDF/PNG 文件（来自 `/paper-figure` ）
4. **LaTeX 包括** — `figures/latex_includes.tex`（来自 `/paper-figure`）
5. **参考书目** — 现有 `.bib` 文件，或将创建一个

如果不存在PAPER_PLAN.md，请用户先运行`/paper-plan`或提供简要概述。

## Orchestra - 引导书写覆盖

保留现有的 `insleep` 工作流程、文件布局和默认值。仅当以下共享参考可以提高写作质量时才使用它们：

- 在起草摘要、引言、相关工作之前，或者当散文感觉很笼统时，请阅读 `../shared-references/writing-principles.md`。
- 在最终撰写和提交准备过程中阅读 `../shared-references/venue-checklists.md`。
- 仅当内置DBLP/CrossRef工作流程不足时才读取`../shared-references/citation-discipline.md`。

这些参考只是支持而已，而不是额外的工作流程阶段。

## 可选：样式参考（ `— style-ref: <source>` ，选择加入）

让用户引导**结构**风格（章节排序、定理密度、句子节奏、图形密度、参考书目风格）走向参考论文。 **默认关闭——当用户没有通过 `— style-ref` 时，与之前不做任何不同的事情。**

仅当 `— style-ref: <source>` 出现在 `$ARGUMENTS` 中时，才先运行辅助程序，然后再起草：

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
  3) echo "error: --style-ref source failed; aborting draft" >&2 ; exit 1 ;;
  *) echo "error: helper failed unexpectedly; aborting draft" >&2 ; exit 1 ;;
esac
```

接受的来源：本地 TeX 目录/文件、本地 PDF 、 arXiv id （ `2501.12345` 或 `arxiv:2501.12345` ）、http(s) URL。  Overleaf URL 和项目 ID 被拒绝 - 首先通过 `/overleaf-sync setup <id>` 克隆并传递本地克隆路径。

**严格的规则**（`tools/extract_paper_style.py` 文档字符串中的完整合同）：

- 仅使用 `style_profile.md` 作为**结构**指导。匹配章节数、章节排序趋势、定理-环境密度、标题-长度分布、句子节奏、数学显示比例、引文风格。
- **切勿从可通过缓存访问的任何内容中复制散文、论断、示例或术语**。个人资料是有意聚合的；如果您需要实质内容，请使用用户自己的大纲。
- **切勿将`— style-ref`（或缓存内容）传递给审稿人/审计分代理。**跨模型审稿独立性（`../shared-references/reviewer-independence.md`）要求审稿人只能看到成果和用户的提示，而不是作者的文体风格。

### `<!-- DATA_NEEDED -->` 标记（当 `GAP_REPORT.md` 存在时）

如果 `/paper-plan` 与 `— style-ref:` 一起运行，它将与 `PAPER_PLAN.md` 一起发出 `GAP_REPORT.md` 。该文件列出了示例暗示的结构槽（消融实验表、缩放实验、故障案例分析等），但用户**没有证据可以填写**。

当 `GAP_REPORT.md` 存在并且节槽被分类为 `status: missing` 时：

1. **不要捏造数字结果、图形参考或定性论断**来填补该空缺。
2. 在缺失内容所在的确切位置发出 HTML 注释占位符：

   ```latex
   <!-- DATA_NEEDED: GAP_S5_ABLATION — ablation table comparing X across the 3 axes implied by exemplar -->
   ```

3. 插槽 ID 和一行描述直接来自 `GAP_REPORT.md` 。 **切勿发明插槽 ID。**切勿将描述改写为比报告更有信心。
4. 该标记特意是 HTML 注释，因此它在呈现的 PDF 中不可见，但 **可通过 `grep -r "DATA_NEEDED" sec/`** 搜索以进行人类分类/`/experiment-bridge` 后续操作。
5. 对于 `status: partial` ，写下用户拥有的内容并在同一段落的间隙点发出 `<!-- DATA_NEEDED: <Slot ID> — <what specifically is short> -->` （不要拆分该部分）。

**从“无占位符”规则中剔除。** 默认的 `/paper-write` 规则（无占位符，例如“参见补充”或“TBD”）仍然适用于所有**除了** GAP_REPORT 列出的缺失插槽。标记是在不损害论断完整性的情况下暴露真实证据缺陷的原则性方法。

原始想法：@zhangpelf in [#217](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/issues/217)。

## 模板

### 目标会议或期刊-特定设置

该技能包括 `templates/` 中的会议模板。根据 TARGET_VENUE 选择：

**ICLR：**
```latex
\documentclass{article}
\usepackage{iclr2026_conference,times}
% \iclrfinalcopy % 取消最终提交版本的注释
```

**神经IPS：**
```latex
\documentclass{article}
\usepackage[preprint]{neurips_2025}
% \usepackage[final]{neurips_2025} %最终提交版
```

**ICML：**
```latex
\documentclass[accepted]{icml2025}
% 使用 [accepted] 作为最终提交版
```

**IEEE 期刊**（交易、信件）：
```latex
\documentclass[journal]{IEEEtran}
\usepackage{cite}  % IEEE uses \cite{}, NOT natbib
% 作者块使用 \author{Name~\IEEEmembership{Member,~IEEE}}
```

**IEEE 会议**（ICC、GLOBECOM、INFOCOM、ICASSP 等）：
```latex
\documentclass[conference]{IEEEtran}
\usepackage{cite}  % IEEE uses \cite{}, NOT natbib
% 作者块使用 \IEEEauthorblockN / \IEEEauthorblockA
```

### 项目结构

生成这个文件结构：

```
paper/
├── main.tex # 主文件（包括部分）
├── iclr2026_conference.sty # 或 neurips_2025.sty / icml2025.sty / IEEEtran.cls + IEEEtran.bst
├── math_commands.tex # 共享数学宏
├── references.bib # 参考书目（已过滤——仅引用的条目）
├──  sections/
│   ├──  0_abstract.tex
│   ├──  1_introduction.tex
│   ├──  2_related_work.tex
│ ├── 3_method.tex # 或预备、设置等
│   ├──  4_experiments.tex
│   ├──  5_conclusion.tex
│ └── A_appendix.tex # 说明详细信息，额外实验
└── figures/ # 符号链接或从项目 figures/ 复制
```

**章节文件灵活**：如果论文计划有 6-8 个章节，请创建相应的文件（例如 `4_theory.tex` 、 `5_experiments.tex` 、 `6_analysis.tex` 、 `7_conclusion.tex` ）。

## 工作流程

### 第0步：备份和清理

如果`paper/`已存在，请在覆盖前备份至`paper-backup-{timestamp}/`。永远不要默默地破坏现有的工作。

**关键：清理陈旧文件。** 当更改节结构（例如，5 个节→ 7 个节）时，删除 `main.tex` 不再引用的节文件。过时的文件（例如，结论移至 `7_conclusion.tex` 时留下的旧 `5_conclusion.tex` ）会导致混乱并浪费空间。

### 第 1 步：初始化项目

1. 创建`paper/`目录
2. 从 `templates/` 复制目标会议或期刊模板 — 该模板已包含：
   - 所有标准包（amsmath、hyperref、cleveref、booktabs 等）
   - 修复了 `\crefname{assumption}` 的定理环境
   - 匿名作者封锁
3. 使用论文特定符号生成 `math_commands.tex`
4. 创建匹配 PAPER_PLAN 结构的节文件

**作者封锁（匿名模式）：**
```latex
\author{Anonymous Authors}
```

### 步骤2：生成math_commands.tex

根据论文的符号创建共享数学宏：

```latex
% math_commands.tex — 共享符号
\newcommand{\R}{\mathbb{R}}
\newcommand{\E}{\mathbb{E}}
\DeclareMathOperator*{\argmin}{arg\,min}
\DeclareMathOperator*{\argmax}{arg\,max}
% 在此处添加论文特定的符号
```

### 第 3 步：写下每个部分

按顺序处理部分。对于每个部分：

1. **阅读计划**——论断、证据、引文属于这里
2. **阅读 NARRATIVE_REPORT.md** — 提取相关内容、发现和定量结果
3. **内容草稿** — 编写完整的 LaTeX（无捏造的占位符）。 **例外：** 如果 `GAP_REPORT.md` 存在并且该部分具有带有 `status: missing` 的插槽，则在这些点发出 `<!-- DATA_NEEDED: <Slot ID> — <description> -->` 而不是发明数据 - 请参阅上面的 DATA_NEEDED 标记小节。
4. **插入 figures/tables** — 使用 `figures/latex_includes.tex` 中的片段
5. **添加引文** — 对于 ML 会议 ( ICLR/NeurIPS/ICML/CVPR/ACL/AAAI )：使用 `\citep{}` / `\citet{}` (natbib)。 **对于 IEEE 会议或期刊**：使用 `\cite{}`（通过 `cite` 包的数字样式）。切勿混合使用 natbib 和 cite 命令。

在起草前面的内容之前，请重新阅读 `PAPER_PLAN.md` 的一句话贡献。在读者了解完整方法之前，摘要和引言应该使这一点显而易见。

#### 特定部分指南

**§0 摘要：**
- 使用 `../shared-references/writing-principles.md` 中的 5 部分流程：什么、为什么难、如何、证据、最强结果
- 必须是独立的（无需阅读论文即可理解）
- 从论文的具体贡献开始，而不是通用的领域背景
- 包括一项具体的定量结果
- 150-250字（检查目标会议或期刊限制）
- 没有引用，没有未定义的缩写词
- 没有 `\begin{abstract}` — 位于 main.tex

**§1 简介：**
- 以引人注目的钩子开头（1-2 句话，问题动机）
- 清楚地说明差距（“但是，……”）
- 在读者陷入细节之前提供简要的方法概述
- 以编号或项目符号列表的形式列出 2-4 具体的、可伪造的贡献
- 尽早预览最强的结果，而不是将其保存到实验部分
- 以简短的路线图结束（“本文的其余部分组织为......”）
- 如果空间允许，请包括主要结果图
- 目标：1-1.5 页
- 方法最迟应从第 2-3 页开始

**§2 相关工作：**
- **至少 1 整页**（3-4 篇实质性段落）。相关工作章节过短是审稿人常见的抱怨。
- 使用 `\paragraph{Category Name.}` 按类别进行组织
- 按假设类别或研究问题按方法论进行组织；不要写逐篇论文的小摘要
- 每个类别： 1 篇文章总结工作路线 + 1-2 句话定位这篇论文
- 不要只列出论文——综合和比较
- 以这篇论文 relates/differs 的方式结束每一段

**§3 方法/准备工作/设置：**
- 尽早定义符号（参考 math_commands.tex ）
- 使用 `\begin{definition}` 、 `\begin{theorem}` 环境进行正式声明
- 理论论文：正文中包含关键结果的证明概要，附录中包含完整证明
- 对于理论论文：包括先前边界与本论文的**比较表**
- 包括算法伪代码（如果适用）（ `algorithm2e` 或 `algorithmic` ）
- 目标：1.5-2 页

**§4 实验：**
- 从实验设置开始（数据集、基线、指标、实施细节）
- 主要结果 table/figure 第一
- 然后消融实验与分析
- 引言中的每一个论断都必须有支持证据
- 对于每个主要实验，明确它支持什么论断以及读者应该注意什么
- 目标：2.5-3 页

**§5 结论：**
- 总结贡献（不是从介绍中复制粘贴 - 改写）
- 限制（2-4 看看，具体限制——数据集规模、计算机制、假设 X；仅限真实的限制，从未发明来填充计数。本节是通用警告的唯一主页）
- 未来的工作（1-2个具体方向）
- 道德声明和再现性声明（如果目标会议或期刊需要）
- 目标：0.5 页

**附录：**
- 证明详细信息（主体定理的完整证明）
- 附加实验，消融实验
- 实现细节、超参数表
- 附加可视化

### 步骤3.5：理论论文一致性通过（仅限理论论文）

在起草所有部分之后和构建参考书目之前运行此遍。

**触发启发式：**如果 `PAPER_PLAN.md` 将其标记为 theory/analysis ，或者起草的部分包含 5 个或更多正式结果环境（ `\begin{theorem}` ， `\begin{lemma}` ， `\begin{proposition}` ， `\begin{corollary}` ），则将论文视为理论重。

**参数源搜索：**在工作区中搜索名称或内容指示规范版本的任何独立完整参数源文件（ `proof` 、 `appendix` 、 `full` 、 `complete` 、 `supplement` 、 `supplementary` ）。如果存在这样的文件，则准确提示用户：

`Inline full proofs from {file}? [Y/n]`

默认为 `Y` 。

如果用户接受：
- 将完整的 theorem/lemma 语句加上证明块导入附录源（`A_appendix.tex` 或计划命名的附录文件）
- 使用主体定理陈述作为规范的公开陈述；附录副本必须与其匹配，除非主体声明在同一遍中被修改
- **不要**留下占位符，例如“参见补充说明文档”或“为简洁起见省略说明”
- 准确保留定理标签、方程标签和证明结构
- 保持主体证明概要简短，但当存在完整的证明源时，切勿让附录成为仅草图的占位符

如果不存在独立的完整说明源：
- 仅当实际写为证明概要时才使用证明概要，而不是占位符
- 请勿伪造外部证明文档参考

**重述审计：**
- 将附录中重述的每条 theorem/lemma/proposition 语句与主体版本进行比较
- 不要比较证明机构；仅审计语句、假设、案例分割、量词、域、符号、变量名称和已定义对象的术语
- 将 `stationary` 与 `terminal` 、更改的假设名称或缺少大小写分割视为不匹配，除非明确记录
- 如果附录需要不同的措辞，请添加明确的符号桥，而不是默默地重命名概念
- 在步骤 4 之前解决所有不匹配问题

**经验动机：**在真实的理论论文运行中，默认行为在附录中生成 `"see supplementary proof document"` 占位符。作者必须从独立的证明文件（例如 `proof_full.tex` ）中手动提取数百行完整的证明。如果没有这一关，理论论文就会附带仅含证明概要的附录，而这些附录在理论会议或期刊上是失败的。

### 第四步：建立参考书目

**重要：仅包含论文中实际引用的条目。**

1. 扫描起草部分中的所有引文参考文献（ML会议为`\citep{}` / `\citet{}`，IEEE会议或期刊为`\cite{}`）
2. 建立引文关键列表
3. 对于每个引用键：
   - 检查 project/narrative 文档中现有的 `.bib` 文件
   - 如果未找到且 **DBLP_BIBTEX = true**，请使用下面经过验证的获取链
   - 如果未找到且 **DBLP_BIBTEX = false**，则搜索 arXiv/Scholar 以获得正确的 BibTeX
   - **切勿伪造 BibTeX 条目** — 用 `[VERIFY]` 注释标记未知条目
4. 写入 `references.bib` 仅包含引用的条目（无膨胀）

#### 已验证 BibTeX 获取（当 DBLP_BIBTEX = true 时）

三步后备链——零安装、零身份验证、全真实 BibTeX ：

**步骤 A：DBLP（最佳质量 — 完整的目标会议或期刊、页面、编辑）**
```bash
# 1.按标题+第一作者检索
curl -s "https://dblp.org/search/publ/api?q=TITLE+AUTHOR&format=json&h=3"
# 2. 从结果中提取 DBLP 标识键（例如 conf/nips/VaswaniSPUJGKP17 ）
# 3. 获取真实的BibTeX
curl -s "https://dblp.org/rec/{key}.bib"
```

**步骤 B：CrossRef DOI（后备 — 适用于 arXiv 预印本）**
```bash
# 如果论文有 DOI 或 arXiv ID ( arXiv DOI = 10.48550/arXiv.{id} )
curl -sLH "Accept: application/x-bibtex" "https://doi.org/{doi}"
```

**步骤 C：标记 `[VERIFY]`（最后手段）** 如果 DBLP 和 CrossRef 均未返回任何内容，请使用 `% [VERIFY]` 注释标记该条目。请勿捏造。

**为什么这很重要：** LLM 生成的 BibTeX 经常会产生目标会议或期刊名称、页码甚至共同作者的幻觉。 DBLP 和 CrossRef 返回经过发布者验证的元数据。上游技能（`/research-lit`，`/novelty-check`）可能会从LLM内存中提到论文——这个获取链是防止幻觉引用进入最终`.bib`的大门。

如果 DBLP/CrossRef 流量不够，请在添加占位符之前加载 `../shared-references/citation-discipline.md` 以实现更严格的回退规则。

**自动围兜清理** — 使用此 Python 模式仅提取引用的条目：

```python
import re
# 1. Grep 所有 .tex 文件中的所有 \citep{...}、\citet{...} 和 \cite{...}
# 2. 提取唯一键（处理多重引用，如 \citep{a,b,c} 或 \cite{a,b,c}）
# 3. 解析完整的.bib文件，只保留key在引用集中的条目
# 4.写入过滤后的号码布
```

这可以防止围兜膨胀（例如，测试中 948 行 → 215 行）。

**强制围兜卫生验证** — 在写入过滤后的 `references.bib` 后立即运行。

```bash
python3 - <<'PY'
import io, json, re, sys, urllib.parse, urllib.request
from pathlib import Path

try:
    import bibtexparser
except ImportError:
    sys.exit("Missing dependency: pip install bibtexparser")

ROOT = Path("paper")
tex_paths = [ROOT / "main.tex", *sorted((ROOT / "sections").glob("*.tex"))]
tex = "\n".join(p.read_text(errors="ignore") for p in tex_paths if p.exists())

cited = set()
for m in re.finditer(r'\\cite[a-zA-Z]*\{([^}]*)\}', tex):
    cited.update(k.strip() for k in m.group(1).split(',') if k.strip())

with (ROOT / "references.bib").open() as fh:
    bib = bibtexparser.load(fh)

entries = {e["ID"]: e for e in bib.entries}
dead = sorted(set(entries) - cited)
if dead:
    print("DEAD ENTRIES:")
    for key in dead:
        print("  ", key)

def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()

def dblp_hits(title):
    q = urllib.parse.quote(title)
    url = f"https://dblp.org/search/publ/api?q={q}&format=json&h=3"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    return [h.get("info", {}) for h in data.get("result", {}).get("hits", {}).get("hit", [])]

def crossref_entry(doi):
    req = urllib.request.Request(f"https://doi.org/{doi}", headers={"Accept": "application/x-bibtex"})
    with urllib.request.urlopen(req, timeout=20) as r:
        parsed = bibtexparser.loads(r.read().decode("utf-8", "ignore"))
    return parsed.entries[0] if parsed.entries else {}

for key in sorted(cited & set(entries)):
    e = entries[key]
    title = e.get("title", "").strip("{}")
    hits = dblp_hits(title) if title else []
    hit = hits[0] if hits else None
    source = "DBLP"
    if hit is None and e.get("doi"):
        try:
            hit = crossref_entry(e["doi"])
            source = "CrossRef"
        except Exception:
            hit = None
    if hit is None:
        print(f"VERIFY {key}: no DBLP/CrossRef hit")
        continue

    issues = []
    year_a = str(e.get("year", "")).strip()
    year_b = str(hit.get("year", "")).strip()
    if year_a and year_b and year_a != year_b:
        issues.append(f"year {year_a} != {year_b}")

    venue_a = e.get("journal") or e.get("booktitle") or ""
    venue_b = hit.get("journal") or hit.get("booktitle") or hit.get("venue") or ""
    if norm(venue_a) and norm(venue_b) and norm(venue_a) != norm(venue_b):
        issues.append(f"venue {venue_a} != {venue_b}")

    authors_a = [norm(a) for a in re.split(r'\s+and\s+', e.get("author", "")) if a.strip()]
    authors_b = [norm(a) for a in re.split(r'\s+and\s+', hit.get("author", "")) if a.strip()]
    if authors_a and authors_b and authors_a[:2] != authors_b[:2]:
        issues.append("author list differs")

    if issues:
        print(f"MISMATCH {key} ({source}): " + "; ".join(issues))
PY
```

如果打印了 `DEAD ENTRIES`，请先从 `references.bib` 中取出这些键，然后再继续。如果打印 `VERIFY` 或 `MISMATCH`，则不要创建元数据：
- 当 DBLP 返回明确的命中时更喜欢它
- 如果 DBLP 未命中并且 DOI 可用，则回退到 CrossRef
- 如果双方不同意或仍然无法验证，则仅保留带有 `% [VERIFY]` 标记的条目
- 未引用的条目必须删除，而不是留下死的参考书目膨胀

**引文可达性规则：** 如果条目的键未出现在 `paper/main.tex` 或任何 `paper/sections/*.tex` 文件中的任何 `\cite...{}` 命令中，则该条目已死亡。

**经验动机：** 在真实的提交运行中，多个死号码布条目在 `references.bib` 中进行了多次改进，并且至少有一个条目存在 key/year 不匹配。现有的自动清洁功能都没有标记出这两种情况。

**引文验证规则（来自claude-scholar + Imbad0202）：**
1. 每个 BibTeX 条目必须包含：作者、标题、年份、venue/journal
2. 优先选择已发布的目标会议或期刊版本而不是 arXiv 预印本（如果已发布）
3. 使用一致的引用键格式：`{firstauthor}{year}{keyword}`（例如，`ho2020denoising`）
4. 仔细检查每个条目的年份和目标会议或期刊
5. 删除重复条目（相同论文但不同键）

### 第 5 步：科学写作质量通过（5 次审核通过）

起草所有部分后，运行五次连续审核。基于塞纳尼的“科学写作”方法论：每个词都必须赢得它的位置。

**第 1 遍：杂波提取** — 将句子剥离为最干净的组件。

| 杂乱的短语 | 替换为 |
|------------------|--------------|
| 由于以下事实： | 因为 |
| 为了 | 到 |
| 一些 | 一些 |
| 值得注意的是 | （删除——只说明要点） |
| 重要的是要注意 | （删除） |
| 目前 | 现在 |
| 基于 | 基于 |
| 鉴于以下事实 | 因为 |
| 有影响 | 影响 |
| 引起 | 原因 |

也去掉多余的：“彻底消除”→“消除”，“未来的计划”→“计划”，“意外的惊喜”→“惊喜”。

消除人工智能主义：深入研究、关键、研究现状、挂毯、下划线、值得注意、有趣。

**第 2 步：主动语态和动词活力** — 识别谁做了什么。

- 现场被动：“to-be”动词+过去分词（“被观察到”，“被分析”）
- 转换：找到行动者，重构为主语-动词-宾语
- 复活被窒息的动词（名词化）：
  - “我们进行了调查”→“我们进行了调查”
  - “系统发生故障”→“系统发生故障”
  - “提供描述”→“描述”

被动语态适用于：既定事实、与代理人无关的方法，或目标会议或期刊风格需要时。

**第 3 关：句子结构** — 结构和流程。改变段落的形状：如果每个段落的运行问题→方法→好处→总结，这种模式就会使读者麻木——打破它。

- 标记句子 > 40 个单词进行拆分
- 确保主语和动词靠近（它们之间没有长括号插入）
- 先放熟悉的上下文，后放新信息
- 将最重要的点放在句子末尾附近
- 让每个段落完成一项工作
- 不要以“This”或“We”开头连续的句子
- 检查段落过渡——每个段落的第一句话应该与前一个句子连接

**第 4 关：关键字一致性** — 香蕉规则。

**不要将“香蕉”称为“细长的黄色水果”，以避免重复。** 如果方法说“肥胖组”，则结果不得切换为“较重组”。技术术语的同义词变化迫使读者怀疑是否引入了新类别。

- 从方法部分提取所有关键术语（组名称、变量名称、技术名称、缩写）
- 验证结果、讨论、表格、图片标题中出现的术语完全相同
- 标记定义术语的每个同义词替换
- 缩写词严格性：标记仅为方便起见而创建的非标准缩写词；验证每个首字母缩略词在第一次使用时都已定义

**通过 5：数字和引文完整性**

- 摘要中的样本量 (N) 是否与表 1 相符？
- 结果中的百分比与表格中的原始数字相符吗？
- 有效数字是否一致且适当？
- 图形与表值相符吗？
- 仅通过二手来源引用的标志统计数据（审稿、教科书）——建议核实主要来源

### 第六步：与REVIEWER_MODEL交叉审稿

将完整草稿发送至 GPT-6-Astra xhigh：

```
mcp__codex__codex :
  型号：GPT-6-Astra
  配置：{“model_reasoning_effort”：“xhigh”}
  提示：|
    审稿本[目标会议或期刊]论文草案（主体，不含附录）。

    判断是否断定两个方向上的校准。建议仅在以下情况下缩小范围：
    当前的范围或方式超出了证据范围；不要要求额外的对冲
    围绕支持的结果。旗帜堆叠树篱，自卫（“我们不
    论断”）、指示声明（“我们不涉及 X”）以及一般警告
    外部限制作为需要删除的书写缺陷。音调修正绝不能改变
    事实、否定、形式、范围、比较方向或数字。

    重点关注：
    1. 引言中的每个论断都有支持证据吗？
    2. 文字是否清晰、简洁且不受人工智能主义影响？
    3. 有任何逻辑差距或不清楚的解释吗？
    4. 它是否适合[MAX_PAGES]页面（到结论结尾）？
    5. 相关工作是否足够全面（≥1 页）？
    6. 对于理论论文：证明概要足够吗？
    7. figures/tables 是否清楚描述并正确引用？
    8. 浏览者能否理解标题、摘要、引言和图 1 中的贡献？

    对于每个问题，请指定：严重性 ( CRITICAL/MAJOR/MINOR )、位置和修复。

    [粘贴完整草稿文本]
```

应用关键和主要修复。为用户记录次要问题。

### 第7步：反向大纲测试（来自研究-论文-写作技能）

起草所有部分后：

1. **提取主题句** — 提取每个段落的第一句
2. **按顺序阅读**——它们应该自己形成一个连贯的叙述
3. **检查论断覆盖率** — 论断证据矩阵中的每个论断都必须出现
4. **检查证据映射** — 每个 experiment/figure 都必须支持声明的论断
5. **修复空白** — 如果主题句不能推动故事发展，请重写段落

### 第 8 步：最终检查

在声明完成之前：

- [ ] 所有 `\ref{}` 和 `\label{}` 匹配（没有未定义的引用）
- [ ] 所有引用命令（ML会议的`\citep{}` / `\citet{}`，IEEE的`\cite{}`）都有相应的BibTeX条目
- [ ] 匿名模式下无作者信息
- [ ] Figure/table 编号正确
- [ ] MAX_PAGES 内的页数（正文到结论结束）
- [ ] 文本中没有留下 TODO/FIXME/XXX 标记
- [ ] 没有未选中的 `[VERIFY]` 标记
- [ ] 摘要是独立的（无需阅读论文即可理解）
- [ ] 标题具体且信息丰富（非通用）
- [ ] 相关作品≥1整页
- [ ] references.bib 仅包含引用的条目（无膨胀）
- [ ] **没有过时的节文件** — `sections/` 中的每个 .tex 都是由 `main.tex` 编辑的 `\input`
- [ ] **节文件匹配 main.tex** — 文件编号和 `\input` 路径一致
- [ ] 满足目标会议或期刊特定要求的 sections/checklists（如果需要，请阅读 `../shared-references/venue-checklists.md`）
- [ ] 浏览者可以从标题、摘要、引言和图 1/captions 中恢复主要论断

## 主要规则

===自信的散文，诚实的限制（永不升级论断）===
1. 根据证据的实际范围和形式校准每个论断，然后直接说明校准的论断。必要的假设、不确定性和范围是论断的一部分；堆叠的树篱和防御性的清嗓子则不然。
2. 如果当前的论断不受支持，请将其缩小到证据支持的版本或删除它。不要用听起来更柔和的同义词来代替固定范围、模式、比较或聚合。
3. 将一般警告和更广泛的边界讨论放在一个限制部分中。在其之外，删除通用免责声明，例如“需要进一步研究”、“可能无法概括”和“应谨慎解释”。 论断-定义范围、假设和统计资格取决于它们所实现的论断。
4. 目标是 2-4 具体限制（数据集规模、计算机制、假设 X）。仅限真实的——永远不要发明一个来满足计数，永远不要笼统地道歉，永远不要在论文中重复相同的限制。
5. 写作说明不是手稿内容。 “Do not提及X”的意思是省略X，而不是写“我们不address/claim/discuss X”。切勿在稿件散文中暴露起草指示、要求省略、审稿人反馈或修订历史。
6. 将自卫（“我们不论断”，“我们的目标仅仅是”）替换为对论文所确定内容的积极的、证据匹配的陈述。如果防御句带有真正的边界，则将该边界保留在论断或限制中；不要删除真实条件内容。
7. 仅语气编辑永远不会改变事实、否定、模态、范围、假设、比较方向、聚合、数字、公式或引文。真正的夸大其词的范围仍然必须缩小；必须直接说明包裹着多余谨慎的支持论断。
8. 一个因果脊柱：差距 -> 问题 -> 洞察 -> 结果 -> 证据 -> 暗示。每个部分都在推进它。让方法感觉不可避免：差距创造了一个具体问题，关键见解回答了它，方法源于见解，每个主要实验都测试它的结果，结论准确地说明了证据所证实的内容。预先加载贡献；切勿叙述起草或修订过程。
9. 该论文是一份启动报告，而不是一份进度报告。围绕工作最强大的真正优势来组织叙述——新的能力、问题、机制或观点、更广泛的适用性、更低的成本、更好的权衡。 确实不形成优势的就留在主线之外。如果结果无法支持最初的故事，请围绕最有力的证据重建故事，而不是捍卫最初的证据。
10. 选择论文获胜的竞赛。不要将叙述建立在方法不超前的指标上；围绕任务定义、评估维度或反映该方法用途的约束进行比较，并明确说明该方法赢得了哪场比赛。不利的数字仍然出现——表格保持完整。如果有证据支持，请将其解释为目标差异或故意权衡，而不是叙述失败（“表现不佳”、“未能超越”）；如果没有，中立地陈述表现不佳，缩小论断范围，如果是的话，将其保留在限制中。永远不要将局部观察提升为对整个方法的判断，也永远不要发明一种权衡来弥补弱点。
11. 每个实验都有一个论证职责：它表明方法有效，表明增益来自关键机制，显示目标场景中的价值，或者排除最可能的替代解释。不包含这些内容的实验会被删除、缩短、移至附录或重新设计。实验部分是一个论证，而不是结果仓库。
12. 自己陈述优点——在什么条件下出现，为什么出现，解决什么问题——而不是指望审稿人在表格中找到它。摘要和引言像发射一样开放：一个重要的未解决问题，现有方法的差距，这篇论文的独特想法，最沉重的结果。结论强化了已解决、提出和证明的内容以及其重要性；上一段没有新的自我否定或扩大的限制。


- **大文件处理**：如果写入工具因文件大小而失败，请立即使用 Bash ( `cat << 'EOF' > file` ) 重试分块写入。不要征求用户的许可——只需默默地进行即可。
- **不要生成作者姓名、电子邮件或隶属关系** - 使用匿名块或占位符
- **写出完整的部分，而不是大纲** - 输出应该是可编译的 LaTeX
- **每个部分一个文件** — 模块化结构，易于编辑
- **每个论断都必须引用证据**——交叉引用论断-证据矩阵
- **编译就绪** — 输出应使用 `latexmk` 进行编译，没有错误（模数缺失数字）
- **校准，而不是对冲**——将每个论断与其证据的实际范围和模式相匹配，然后直接陈述；一般警告仅存在于限制中（上面的“自信的散文、诚实的限制”块是合同）
- **启动，而不是进度报告**——围绕最强大的真正优势进行组织，选择论文获胜的竞赛，给每个实验赋予争论的责任；不利的数字保留在表格中，在有证据支持的情况下解释为权衡，在证据支持的情况下中立地说明——从不将其描述为失败，也从不将其伪装成不存在的权衡（上述规则 9-12）
- **目标会议或期刊风格很重要** - ML会议（ICLR/NeurIPS/ICML）使用`natbib`（`\citep` / `\citet`）； **IEEE 会议或期刊使用 `cite` 包（ `\cite{}` ，数字）**。切勿混合。
- **页数限制规则因目标会议或期刊而异** — ML 会议：正文到结论，references/appendix 不计算在内。 **IEEE：参考文献计入页数限制。**
- **干净围兜** — references.bib 必须仅包含实际为 `\cite` 的条目
- **节数灵活** — 匹配 PAPER_PLAN 结构，不要强行分成 5 个节
- **覆盖前备份** — 切勿在不备份的情况下破坏现有的 `paper/` 目录
- **预先加载贡献** - 在实验或附录之前不要隐藏收益
- **按参数排序结果，而不是按实验室笔记本** - 按照最能构建案例的顺序呈现实验，而不是按照它们运行时发生的顺序
- **控制和消融实验位于他们测试的论断旁边** - 没有集中在一个遥远的小节中，读者已经忘记了其中的利害关系

## 写作质量参考

- `../shared-references/writing-principles.md` — 故事框架、abstract/introduction 模式、句子层次清晰度、审稿人阅读顺序
- `../shared-references/venue-checklists.md` — 在声明完成之前检查 ICLR/NeurIPS/ICML/IEEE 提交要求
- `../shared-references/citation-discipline.md` — 对不明确的引用进行更严格的后备

继续使用上面主要工作流程中的反向轮廓测试和抗通胀抛光；共享参考是为了在不添加新阶段的情况下提高质量。

## 致谢

写作方法改编自[Research-paper-Writing-Skills](https://github.com/Master-cai/Research-Paper-Writing-Skills)（CCF获奖方法）。来自 [claude-scholar](https://github.com/Galaxy-Dawn/claude-scholar) 和 [ Imbad0202/academic-research-skills ](https://github.com/Imbad0202/academic-research-skills) 的引文验证。该混合包的写作指导覆盖层改编自 Orchestra Research 的论文写作材料。

