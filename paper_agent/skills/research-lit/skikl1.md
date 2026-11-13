---
name: research-lit
description: "搜索和分析研究论文，查找相关工作，总结关键思想。当用户说“查找论文”、“相关工作”、“文献综述”、“这篇论文说了什么”或需要了解学术论文时使用。"
argument-hint: "[paper-topic-or-url]"
allowed-tools: Bash(*), Read, Glob, Grep, WebSearch, WebFetch, Write, Agent, mcp__zotero__*, mcp__obsidian-vault__*
---

# 研究文献综述

研究课题：$ARGUMENTS

## 常量


- **PAPER_LIBRARY** — 包含用户论文集 (PDF) 的本地目录。按顺序检查这些路径：
  1. 当前工程目录下的`papers/`
  2. 当前工程目录下的`literature/`
  3. `## Paper Library`下的`CLAUDE.md`中用户指定的自定义路径
- **MAX_LOCAL_PAPERS = 20** — 要扫描的本地 PDF 的最大数量（每个读取前 3 页）。如果找到更多，请按文件名与主题的相关性确定优先级。
- **SOURCES = `all`** — 要搜索的文献来源。选项：`zotero`、`obsidian`、`local`、`web`、`semantic-scholar`、`deepxiv`、`exa`、`gemini`、`openalex`、 `all`。完整源表和选择规则：请参阅下面的 `## Data Sources`。
- **ARXIV_DOWNLOAD = false** — 当 `true` 时，搜索后将前 3-5 个最相关的 arXiv PDF 下载到 PAPER_LIBRARY。当 `false`（默认）时，仅通过 arXiv API 获取元数据（标题、摘要、作者）——不下载任何文件。
- **ARXIV_MAX_DOWNLOAD = 5** — `ARXIV_DOWNLOAD = true` 时下载的最大 PDF 数量。

> 💡 覆盖：
> - `/research-lit "topic" — paper library: ~/my_papers/` — 自定义本地 PDF 路径
> - `/research-lit "topic" — sources: zotero, local` — 仅搜索 Zotero + 本地 PDF
> - `/research-lit "topic" — sources: web` — 仅搜索网络（跳过所有本地）
> - `/research-lit "topic" — sources: web, semantic-scholar` — 还可以搜索 Semantic Scholar 来查找已发布的目标会议或期刊论文（IEEE、ACM 等）
> - `/research-lit "topic" — sources: all, deepxiv` — 使用默认源加上 DeepXiv
> - `/research-lit "topic" — arxiv download: true` — 下载最相关的 arXiv PDF
> - `/research-lit "topic" — arxiv download: true, max download: 10` — 最多下载 10 个 PDF

## 数据来源

此技能**按优先级**检查多个来源。所有这些都是可选的 - 如果未配置或未请求源，则默默地跳过它。

### 来源选择

解析 `$ARGUMENTS` 以获得 `— sources:` 指令：
- **如果指定了 `— sources:`**：仅搜索列出的源（以逗号分隔）。有效值： `zotero` 、 `obsidian` 、 `local` 、 `web` 、 `semantic-scholar` 、 `deepxiv` 、 `exa` 、 `gemini` 、 `openalex` 、 `all`。
- **如果未指定**：默认为 `all` — 按优先级顺序搜索每个可用源（ `semantic-scholar` 、 `deepxiv` 、 `exa` 、 `gemini` 和 `openalex` 从 `all` 中**排除**；必须明确列出它们）。

示例：
```
/research-lit“扩散型号”→全部（默认，无S2）
/research-lit“扩散模型” — 来源：全部 → 全部（默认，无 S2）
/research-lit“扩散模型” — 来源：zotero → 仅 Zotero
/research-lit“扩散模型” — 来源：zotero、网络 → Zotero + 网络
/research-lit“扩散模型” — 来源：本地 → 仅本地 PDF
/research-lit “主题” — 来源：黑曜石、本地、网络 → 跳过 Zotero
/research-lit "topic" — 来源：网络、语义学者 → web + S2 API ( IEEE/ACM 目标会议或期刊论文)
/research-lit“主题” — 来源：deepxiv → 仅 DeepXiv
/research-lit “主题” — 来源：全部、deepxiv → 默认来源 + DeepXiv
/research-lit “主题” — 来源：全部、语义学者 → 全部 + S2 API
/research-lit“主题” — 来源：exa → 仅 Exa（广域网 + 内容提取）
/research-lit “主题” — 来源：全部、exa → 默认来源 + Exa 网络搜索
/research-lit“主题”——来源：gemini → 仅 Gemini（人工智能驱动的广泛发现）
/research-lit “主题” — 来源：全部、双子座 → 默认来源 + Gemini 发现
/research-lit “主题” — 来源：gemini、语义学者 → Gemini + S2（广泛发现 + 目标会议或期刊元数据）
/research-lit“主题” — 来源：openalex → 仅 OpenAlex（开放引用图 + 机构）
/research-lit “主题” — 来源：semantic-scholar、openalex → S2 + OpenAlex（补充元数据）
```

### 源表

| 优先事项 | 来源 | ID | 如何检测 | 它提供什么 |
|----------|--------|----|---------------|-----------------|
| 1 | **Zotero**（通过 MCP ） | `zotero` | 尝试调用任何 `mcp__zotero__*` 工具 - 如果不可用，请跳过 | 集合、标签、注释、PDF 亮点、BibTeX 、语义搜索 |
| 2 | **Obsidian**（通过 MCP ） | `obsidian` | 尝试调用任何 `mcp__obsidian-vault__*` 工具 - 如果不可用，请跳过 | 研究笔记、论文摘要、标记参考文献、维基链接 |
| 3 | **本地 PDF** | `local` | `Glob: papers/**/*.pdf, literature/**/*.pdf` | 原始 PDF 内容（前 3 页） |
| 4 | **网络搜索** | `web` | 始终可用（网络搜索） | arXiv ,  Semantic Scholar ,  Google Scholar |
| 5 | **Semantic Scholar   API** | `semantic-scholar` | `$S2_FETCHER` 解析（规范名称 `semantic_scholar_fetch.py` ，根据集成合同§2） | 已发表目标会议或期刊论文（IEEE、ACM、Springer），包含结构化元数据：引用计数、目标会议或期刊信息、TLDR。 **仅在通过 `— sources: semantic-scholar` 或 `— sources: web, semantic-scholar` 明确请求时运行** |
| 6 | **DeepXiv CLI** | `deepxiv` | `$DEEPXIV_FETCHER` 解析（规范名称 `deepxiv_fetch.py` ，根据集成合同第 2 条）**和** `deepxiv` CLI 存在（ `command -v deepxiv` ） | 渐进式论文检索：搜索、摘要、标题、章节、趋势、网络搜索。 **仅在通过 `— sources: deepxiv` 或 `— sources: all, deepxiv` 明确请求时运行** |
| 7 | **Exa搜索** | `exa` | `$EXA_FETCHER` 解析（规范名称 `exa_search.py` ，根据集成合同§2）； fetcher 在内部处理 `exa-py` SDK + API 密钥 | 人工智能驱动的广泛网络搜索，具有内容提取功能（亮点、文本、摘要）。涵盖 arXiv/S2. 之外的博客、文档、新闻、公司和研究论文 **仅在通过 `— sources: exa` 或 `— sources: all, exa` 明确请求时运行** |
| 8 | **Gemini** ( MCP / CLI) | `gemini` | 可用 `mcp__gemini-cli__ask-gemini` 工具，或安装 `gemini` CLI | 人工智能驱动的广泛文献发现——将主题分解为子问题、别名和变体，以进行更广泛的检索。更喜欢 MCP ，回退到 CLI。 **仅在通过 `— sources: gemini` 或 `— sources: all, gemini` 明确请求时运行** |
| 9 | **OpenAlex** | `openalex` | `$OPENALEX_FETCHER` 解析（规范名称 `openalex_fetch.py` ，根据集成合同§2）**和** Python `requests` 模块可导入 | 开放引文图，包含 2.5 亿多件作品的机构隶属关系、资助数据和综合元数据。全开API。 **仅在通过 `— sources: openalex` 或 `— sources: all, openalex` 明确请求时运行** |

> **优雅降级**：如果未配置 MCP 服务器，则该技能的工作方式与以前完全相同（本地 PDF + 网络搜索）。  Zotero和Obsidian是纯添加物。

## 工作流程

### 步骤 0a：搜索 Zotero 库（如果有）

**如果未配置 Zotero MCP，则完全跳过此步骤。**

尝试调用 Zotero MCP 工具（例如搜索）。如果成功：

1. **按主题搜索**：使用Zotero搜索工具查找与研究主题匹配的论文
2. **阅读收藏**：检查用户是否有与该主题相关的collection/folder
3. **提取注释**：对于高度相关的论文，提取 PDF 突出显示和注释 - 这些代表用户认为重要的内容
4. **导出BibTeX**：获取相关论文的引文数据（稍后对`/paper-write`有用）
5. **编译结果**：对于每个相关的 Zotero 条目，提取：
   - 标题、作者、年份、目标会议或期刊
   - 用户annotations/highlights（如有）
   - 用户分配的标签
   - 属于哪个集合

> 📚 Zotero 注释是黄金——它们显示了用户个人强调的重要内容，这比通用摘要更有价值。

### 步骤 0b：搜索 Obsidian Vault（如果有）

**如果未配置 Obsidian MCP，则完全跳过此步骤。**

尝试调用 Obsidian MCP 工具（例如搜索）。如果成功：

1. **搜索库**：搜索与研究主题相关的笔记
2. **检查标签**：查找标有相关主题的注释（例如， `#diffusion-models` 、 `#paper-review` ）
3. **阅读研究笔记**：相关笔记，提取用户自己的总结和见解
4. **关注链接**：如果注释链接到其他相关注释（维基链接），请关注它们以获取其他上下文
5. **编译结果**：对于每个相关注释：
   - 注意标题和路径
   - 用户 summary/insights
   - 其他笔记的链接（研究图）
   - 任何 frontmatter 元数据（论文 URL、状态、评级）

> 📝 Obsidian 笔记代表了用户的**加工后的理解**——对于理解他们的观点比原始论文内容更有价值。

### 步骤0c：扫描本地论文库

网上搜索之前，先检查用户本地是否已经有相关论文：

1. **定位库**：检查 PDF 文件的 PAPER_LIBRARY 路径
   ```
   通配模式:  papers/**/*.pdf ,  literature/**/*.pdf
   ```

2. **针对 Zotero 进行重复数据删除**：如果步骤 0a 找到论文，则跳过 Zotero 结果已涵盖的任何本地 PDF（按文件名或标题匹配）。

3. **按相关性过滤**：将文件名和首页内容与研究主题进行匹配。跳过明显不相关的论文。

4. **总结相关论文**：对于每个相关的局部 PDF （最多 MAX_LOCAL_PAPERS ）：
   - 阅读前 3 页（标题、摘要、简介）
   - 摘录：标题、作者、年份、核心贡献、与主题的相关性
   - 标记直接相关与切向相关的论文

5. **建立本地知识库**：将摘要编译到“您已有的论文”部分。这成为起点——外部搜索填补了空白。

> 📚 如果用户本地收藏比较全面，外部搜索可以更有针对性（重点关注缺失的内容）。
> ⚠️ **如果所有三个 PAPER_LIBRARY 路径均未命中，请在继续之前说明** - 不要默默跳过。如果用户的 PDF 位于参考管理器（ Zotero 、Mendeley 等）中，则假定 `— sources: all` 覆盖了它们。发射：
> `WARN: local contributed nothing — no PDFs found in papers/, literature/, or a configured paper library. To include yours, add a "## Paper Library" heading to CLAUDE.md followed by the directory path.`
> 然后继续执行步骤 1。

### 第 1 步：搜索（外部）
- 使用 WebSearch 查找有关该主题的最新论文
- 检查 arXiv 、 Semantic Scholar 、 Google Scholar
- 除非研究基础工作，否则专注于过去两年的论文
- **去重复**：跳过 Zotero 、 Obsidian 或本地库中已找到的论文

**arXiv API 搜索**（在未设置 `— sources:` 时运行，包含 `web` 或 `all` ；默认情况下不下载 - arXiv API 是 Priority-4 Web 层的一部分，请参阅上面的源表）：

**策略 D2 跟踪规则（协调器管理）**：执行者（您，LLM）维护上下文中的贡献源列表。对于**辅助程序支持的 bash 源**（arxiv、semantic-scholar、deepxiv、exa、openalex），只要其 bash 块成功运行其辅助程序，源就会做出贡献（辅助程序已解析并且调用已退出 0；注意：退出 0 且结果列表为空的辅助程序仍算作“已运行”——下游相关性排名决定用户是否真正看到内容）。对于**非辅助程序源**（zotero / obsidian / local PDF / WebSearch / Gemini ），贡献规则在下面的 Step-1 最终确定块中说明 - 这些是单独跟踪的，因为它们不会从 bash 发出 `D2 contribution:` 日志行。未通过 `— sources:` 请求的来源不计入在内。在步骤 1 结束时（在“可选 PDF 下载”之前），如果贡献的源为零，则出现 D2 空聚合错误并停止。 （参见 integration-contract.md §2 策略 D2 — 上下文跟踪取代了共享的 bash 累加器，因为 SKILL bash 块作为单独的 shell 执行；状态无法保留。）

通过规范链解析 `$ARXIV_FETCHER`（策略 D2 — 此源有助于多源聚合；失败时警告并继续，绝不中止整个聚合）：

```bash
# 规范严格安全解析器（参见 shared-references/integration-contract.md §2）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
ARXIV_FETCHER=".aris/tools/arxiv_fetch.py"
[ -f "$ARXIV_FETCHER" ] || ARXIV_FETCHER="tools/arxiv_fetch.py"
[ -f "$ARXIV_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && ARXIV_FETCHER="$ARIS_REPO/tools/arxiv_fetch.py"; }
[ -f "$ARXIV_FETCHER" ] || ARXIV_FETCHER=""

if [ -n "$ARXIV_FETCHER" ]; then
  # 搜索 arXiv API 获取结构化结果（标题、摘要、作者、类别）。
  # 用 if/then/else 包裹起来，这样 set -e 就不会中止技能。
  if python3 "$ARXIV_FETCHER" search "QUERY" --max 10; then
    echo "D2 contribution: arxiv (helper invocation exit 0)" >&2
  else
    echo "WARN: arxiv_fetch.py invocation failed; D2 aggregate continues with WebSearch results." >&2
  fi
else
  echo "WARN: arxiv_fetch.py not resolved; falling back to WebSearch for arXiv hits." >&2
fi
```

> **记录保存**：跟踪每个源的 bash 块发出的 `D2 contribution: …` 行。它们构成了编排器用于下面第 1 步最终确定门的贡献源列表。当 WebSearch 被请求（没有 `— sources:` 过滤器，或者列表包含 `web` 或 `all` ）并且实际被调用时，WebSearch（优先级 4）被视为已做出贡献；协调器单独记录。 （下面的最终确定块规范地重申了此规则 - 两行必须保持同步。）

如果 `$ARXIV_FETCHER` 为空（D2 正常降级），则回退到 WebSearch 查找 arXiv（与之前相同）。

arXiv API 返回结构化元数据（标题、摘要、完整作者列表、类别、日期）——比 WebSearch 片段更丰富。将这些结果与 WebSearch 结果合并并消除重复。

**Semantic Scholar API 搜索**（仅当 `semantic-scholar` 位于源中时）：

当用户明确请求 `— sources: semantic-scholar` （或 `— sources: web, semantic-scholar` ）时，搜索 arXiv 之外已发布的目标会议或期刊论文：

```bash
# 重新解析 $ARIS_REPO（SKILL bash 块可能在单独的 shell 中运行）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
# 解决 $S2_FETCHER（策略 D2 — 丢失时发出警告并跳过）。
S2_FETCHER=".aris/tools/semantic_scholar_fetch.py"
[ -f "$S2_FETCHER" ] || S2_FETCHER="tools/semantic_scholar_fetch.py"
[ -f "$S2_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && S2_FETCHER="$ARIS_REPO/tools/semantic_scholar_fetch.py"; }
[ -f "$S2_FETCHER" ] || S2_FETCHER=""

if [ -n "$S2_FETCHER" ]; then
  # 使用质量过滤器搜索已发表的 CS/Engineering 论文。
  # 用 if/then/else 包裹起来，这样 set -e 就不会中止技能。
  if python3 "$S2_FETCHER" search "QUERY" --max 10 \
      --fields-of-study "Computer Science,Engineering" \
      --publication-types "JournalArticle,Conference"; then
    echo "D2 contribution: semantic_scholar (helper invocation exit 0)" >&2
  else
    echo "WARN: semantic_scholar_fetch.py invocation failed; D2 aggregate continues with remaining sources." >&2
  fi
fi
```

如果 `$S2_FETCHER` 为空（规范链已耗尽），则静默跳过 — D2 多源聚合继续处理剩余的已解析源。

**为什么使用Semantic Scholar？** 许多IEEE/ACM期刊论文不在arXiv上。 S2 通过引用计数和目标会议或期刊元数据填补了已发表的仅目标会议或期刊论文的空白。

**arXiv 和 S2 之间的去重**：通过 arXiv ID 进行匹配（S2 返回 `externalIds.ArXiv` ）：
- 如果一篇论文出现在两者中：检查S2的`venue` / `publicationVenue` - 如果它已在journal/conference（例如IEEE TWC，JSAC）中发表，则使用S2的元数据（目标会议或期刊，引用计数，DOI）作为权威版本，因为已发布的版本取代预印本。保留 arXiv PDF 链接以供下载。
- 如果 S2 匹配没有目标会议或期刊（仍然只是 S2 索引的预印本）：保持 arXiv 版本不变。
- 没有 `externalIds.ArXiv` 的 S2 结果是**目标会议或仅限期刊的论文**，不在 arXiv 上——这些是该来源的独特价值。

**DeepXiv 搜索**（仅当 `deepxiv` 位于源中时）：

当用户显式请求 `— sources: deepxiv`（或在组合源列表中包含 `deepxiv`）时，使用 DeepXiv 适配器进行渐进检索：

```bash
# 重新解析 $ARIS_REPO（SKILL bash 块可能在单独的 shell 中运行）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
# 解决 $DEEPXIV_FETCHER（策略 D2 — 丢失时发出警告并跳过）。
DEEPXIV_FETCHER=".aris/tools/deepxiv_fetch.py"
[ -f "$DEEPXIV_FETCHER" ] || DEEPXIV_FETCHER="tools/deepxiv_fetch.py"
[ -f "$DEEPXIV_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && DEEPXIV_FETCHER="$ARIS_REPO/tools/deepxiv_fetch.py"; }
[ -f "$DEEPXIV_FETCHER" ] || DEEPXIV_FETCHER=""

if [ -n "$DEEPXIV_FETCHER" ] && command -v deepxiv >/dev/null 2>&1; then
  # 包装每个适配器调用，以便 set -e 不会中止 SKILL。
  if python3 "$DEEPXIV_FETCHER" search "QUERY" --max 10; then
    echo "D2 contribution: deepxiv (helper invocation exit 0)" >&2

    # 然后仅深化最相关的论文（子调用不会改变 D2 聚合计数）：
    python3 "$DEEPXIV_FETCHER" paper-brief ARXIV_ID \
      || echo "WARN: deepxiv_fetch.py paper-brief failed; skipping deepen step." >&2
    python3 "$DEEPXIV_FETCHER" paper-head ARXIV_ID \
      || echo "WARN: deepxiv_fetch.py paper-head failed; skipping deepen step." >&2
    python3 "$DEEPXIV_FETCHER" paper-section ARXIV_ID "Experiments" \
      || echo "WARN: deepxiv_fetch.py paper-section failed; skipping deepen step." >&2
  else
    echo "WARN: deepxiv_fetch.py search invocation failed; D2 aggregate continues with remaining sources." >&2
  fi
fi
```

如果 `$DEEPXIV_FETCHER` 为空或 `deepxiv` CLI 不可用，请优雅地跳过此源并继续处理其余请求的源（策略 D2 优雅降级）。

**为什么使用 DeepXiv？** 当广泛搜索之后应该进行分阶段阅读而不是立即加载全文时，它很有用。这减少了不必要的上下文，同时仍然呈现结构、TLDR 和最相关的部分。

**针对 arXiv 和 S2 的重复数据删除**：
- 首先按 arXiv ID 匹配，其次按 DOI，第三按标准化标题匹配
- 如果 DeepXiv 和 arXiv 引用相同的预印本，则保留一个规范论文行并记录 `deepxiv` 作为附加来源
- 如果 DeepXiv 与已发表的论文中的 S2 重叠，则优先选择最终表中的 S2 venue/citation 元数据，但在添加值时保留 DeepXiv 派生的部分注释

**Exa 搜索**（仅当 `exa` 位于源中时）：

当用户明确请求 `— sources: exa`（或在组合源列表中包含 `exa`）时，请使用 Exa 工具进行广泛的 AI 支持的 Web 搜索和内容提取：

```bash
# 重新解析 $ARIS_REPO（SKILL bash 块可能在单独的 shell 中运行）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
# 解决 $EXA_FETCHER（策略 D2 — 丢失时发出警告并跳过）。
EXA_FETCHER=".aris/tools/exa_search.py"
[ -f "$EXA_FETCHER" ] || EXA_FETCHER="tools/exa_search.py"
[ -f "$EXA_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && EXA_FETCHER="$ARIS_REPO/tools/exa_search.py"; }
[ -f "$EXA_FETCHER" ] || EXA_FETCHER=""

if [ -n "$EXA_FETCHER" ]; then
  # 搜索有亮点的研究论文。
  # 用 if/then/else 包裹起来，这样 set -e 就不会中止技能。
  exa_contributed=false
  if python3 "$EXA_FETCHER" search "QUERY" --max 10 --category "research paper" --content highlights; then
    exa_contributed=true
  else
    echo "WARN: exa_search.py research-paper invocation failed; D2 aggregate continues." >&2
  fi
  # 搜索更广泛的网络内容（博客、文档、新闻）
  if python3 "$EXA_FETCHER" search "QUERY" --max 10 --content highlights; then
    exa_contributed=true
  else
    echo "WARN: exa_search.py broad-web invocation failed; D2 aggregate continues." >&2
  fi
  [ "$exa_contributed" = "true" ] && echo "D2 contribution: exa (at least one invocation exit 0)" >&2
fi
```

如果 `$EXA_FETCHER` 为空或 `exa-py` SDK 不可用，请优雅地跳过此源并继续处理其余请求的源（策略 D2 优雅降级）。

**为什么使用 Exa？** Exa 通过内置内容提取在更广泛的网络（博客、文档、新闻、公司页面）中提供人工智能驱动的搜索。它通过为每个结果返回更丰富的内容来填补学术数据库（arXiv，S2）和通用 WebSearch 之间的空白。

**针对 arXiv、S2 和 DeepXiv 的重复数据删除**：
- 首先按 URL 匹配，然后标准化标题
- 如果 Exa 返回 arXiv 论文已被 arXiv/S2 找到，则更喜欢来自这些来源的结构化元数据
- 来自非学术领域（博客、文档、新闻）的 Exa 结果具有其他来源未涵盖的独特价值

**Gemini 搜索**（仅当 `gemini` 位于源中时）：

当用户明确请求 `— sources: gemini`（或在组合源列表中包含 `gemini`）时，使用 Gemini 进行 AI 支持的广泛文献发现。

**优先级 1 — Gemini MCP**（首选）：使用搜索提示调用 `mcp__gemini-cli__ask-gemini`：

```
mcp__gemini-cli__ask-gemini ({
  提示：“你是一名研究文献侦察员。全面搜索论文：“QUERY”

重要限制：
1. 从多个角度搜索 - 将主题分解为子问题、别名、相邻任务和常见的 benchmark/settings 变体。
2. 更喜欢真正相关的论文，而不仅仅是关键词相邻的论文。
3. 包括热门会议或期刊、期刊、调查、最近的预印本和论文以及可用的代码。
4. 从 2022 年开始专注于论文，除非需要较早的基础工作。

对于找到的每篇论文，请提供以下所有信息：
- 标题：[确切标题]
- 作者：[完整作者列表]
- 年份：[出版年份]
- 目标会议或期刊：[确切的conference/journal名称+年份，或“arXiv预印本”]
- arXiv ID：[格式2401.12345，或“N/A”]
- DOI：[如果有，或“ N/A ”]
- 代码 URL：[ GitHub/GitLab 链接（如果有），或“无代码”]
- 摘要：【一句话核心贡献】

找到至少 15 篇论文。',
  型号：'auto-gemini-3'
})
```

**优先级 2 — Gemini CLI 后备**（如果 MCP 不可用）：通过 Bash 使用 `gemini -p "...same prompt..." 2>/dev/null`（超时：120 秒）。

如果 MCP 和 CLI 都不可用，请优雅地跳过此源并继续使用其余请求的源。

**为什么使用 Gemini？** Gemini 提供人工智能驱动的发现，超越关键字匹配 - 它分解主题，探索命名变体，并显示基于 API 的传统搜索（arXiv，S2）可能会错过的论文。它填补了与结构化数据库查询不同的检索领域。

**针对 arXiv 、S2、DeepXiv 和 Exa 的重复数据删除**：
- 首先按 arXiv ID 匹配，其次按 DOI，第三按标准化标题匹配
- 如果 Gemini 返回 S2 已找到的论文，则优先选择 S2 的引用计数和目标会议或期刊元数据
- 如果 Gemini 返回 arXiv 已找到的论文，则更喜欢 arXiv 的结构化元数据
- Gemini 的独特价值在于发现其他基于关键字的索引没有出现的论文
- **请勿使用 Gemini 报告的引用计数** — 它们可能不准确。使用S2获取权威引文数据。

**OpenAlex 搜索**（仅当 `openalex` 位于源中时）：

当用户明确请求 `— sources: openalex`（或在组合源列表中包含 `openalex`）时，请使用 OpenAlex API 获取全面的学术元数据：

```bash
# 重新解析 $ARIS_REPO（SKILL bash 块可能在单独的 shell 中运行）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
# 解决 $OPENALEX_FETCHER（策略 D2 — 丢失时发出警告并跳过）。
OPENALEX_FETCHER=".aris/tools/openalex_fetch.py"
[ -f "$OPENALEX_FETCHER" ] || OPENALEX_FETCHER="tools/openalex_fetch.py"
[ -f "$OPENALEX_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && OPENALEX_FETCHER="$ARIS_REPO/tools/openalex_fetch.py"; }
[ -f "$OPENALEX_FETCHER" ] || OPENALEX_FETCHER=""

# 预检：如果辅助程序未解决或出现问题，则静默跳过 OpenAlex
# `requests` Python 包丢失。两项检查必须在之前通过
# 该脚本被调用，因此没有安装 `requests` 的用户永远不会看到
# 默认 `/research-lit` 运行的堆栈跟踪。
if [ -z "$OPENALEX_FETCHER" ] || ! python3 -c "import requests" >/dev/null 2>&1; then
  echo "OpenAlex source not available (openalex_fetch.py unresolved or 'requests' module missing); skipping." >&2
else
  # 搜索具有全面元数据的论文。
  # 用 if/then/else 包裹起来，这样 set -e 就不会中止技能。
  if python3 "$OPENALEX_FETCHER" search "QUERY" --max 10 \
      --year "2022-" \
      --type article \
      --sort relevance; then
    echo "D2 contribution: openalex (helper invocation exit 0)" >&2
  else
    echo "WARN: openalex_fetch.py invocation failed; D2 aggregate continues with remaining sources." >&2
  fi
fi
```

如果未找到 `openalex_fetch.py` 或缺少 `requests` 模块，请优雅地跳过此源并继续处理其余请求的源。

**为什么使用 OpenAlex ？** 完全开放的引文图（不需要 API 密钥）、机构隶属关系、资助数据（NSF、NIH）、全面的 topic/keyword 元数据以及跨所有学科（不仅仅是 CS）的覆盖范围。

**针对 arXiv、S2、DeepXiv、Exa 和 Gemini 进行重复数据删除**：
- 首先按 DOI 匹配（大多数作品都有 OpenAlex 的 DOI），然后是 arXiv ID，然后是标准化标题
- 如果OpenAlex和S2都有相同的论文：
  - 优先考虑 S2 的引用计数（更新）
  - 首选 S2 作为目标会议或期刊元数据（对于 CS/AI 论文更准确）
  - 使用 OpenAlex 获取机构隶属关系和资助数据（唯一价值）
  - 将两者合并为更丰富的记录
- 如果 OpenAlex 和 arXiv 重叠，则首选 arXiv 的 PDF 链接和元数据，但保留 OpenAlex 的 citation/institution 数据
- OpenAlex的独特价值：机构隶属关系、资金来源、主题分类全面、学科覆盖

**D2 总体最终确定**（根据集成合同 §2 政策 D2）：

协调器（您， LLM ）通过读取上面每个源的 bash 块发出的 `D2 contribution: <name>` 日志行来维护上下文贡献源列表，加上：

- 如果步骤 0a 返回非空 Zotero 命中，则为 `zotero`。
- 如果步骤 0b 返回非空 Obsidian 命中，则为 `obsidian`。
- `local` 如果步骤 0c 找到至少一个相关的本地 PDF 。
- `web` 如果请求了 WebSearch（优先级 4）（没有 `— sources:` 过滤器，或者列表包含 `web` 或 `all` ）并且实际被调用。注意：`— sources: all` 涵盖默认层（zotero、obsidian、local、web）——它**不**包括选择加入的获取器（semantic-scholar、deepxiv、exa、gemini、openalex）。要启用这些功能，请显式添加它们（例如 `— sources: all, semantic-scholar, openalex` ），以匹配此技能的 L42-43 / L51-63 处的现有约定。
- `gemini` 如果 Gemini MCP / CLI 返回至少一篇论文。

如果生成的贡献源列表有零个条目，则显示：

> **错误**：D2 聚合为空 — 每个请求的源要么未解析、未调用、失败，要么（对于 MCP / 本地 PDF / Gemini 源）未返回可用结果。 （注意：即使结果集为空，WebSearch 也会在请求和调用时做出贡献。）多源聚合无法继续。建议用户使用更广泛的 `— sources:` 列表（例如 `web, local` ）重试或检查辅助程序分辨率和 SDK 安装。

然后在步骤 1.5 之前停止。否则，将贡献源列表记录给用户（例如“贡献的源：arxiv、semantic_scholar、web”）并继续。

**可选PDF下载**（仅当`ARXIV_DOWNLOAD = true`时）：

搜索完所有来源并按相关性对论文进行排名后：
```bash
# 重新解析 $ARXIV_FETCHER（SKILL bash 块可能在单独的 shell 中运行）。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
ARXIV_FETCHER=".aris/tools/arxiv_fetch.py"
[ -f "$ARXIV_FETCHER" ] || ARXIV_FETCHER="tools/arxiv_fetch.py"
[ -f "$ARXIV_FETCHER" ] || { [ -n "${ARIS_REPO:-}" ] && ARXIV_FETCHER="$ARIS_REPO/tools/arxiv_fetch.py"; }
[ -f "$ARXIV_FETCHER" ] || ARXIV_FETCHER=""

# 下载前 N 个最相关的 arXiv 论文；如果辅助程序未解决，请静默跳过。
[ -n "$ARXIV_FETCHER" ] && python3 "$ARXIV_FETCHER" download ARXIV_ID --dir papers/
```
- 只下载相关性排名靠前的论文 ARXIV_MAX_DOWNLOAD
- 跳过本地图书馆已有的论文
- 下载之间有 1 秒延迟（速率限制）
- 验证每个 PDF > 10 KB

### 步骤1.5：验证候选论文（抗幻觉，强制）

在分析之前，对从步骤 0a-1 收集的**所有**候选论文进行运行预搜索验证，以过滤掉 LLM 伪造的 arXiv ID / DOI / 标题。 辅助程序：`verify_papers.py`（规范名称；根据 [ `shared-references/integration-contract.md` ](../shared-references/integration-contract.md ) §2 解决，政策 D1 — 具有降级输出回退的主要辅助程序）。如果辅助程序在此机器上未解决，则 SKILL 会发出后备 `verified_papers.json` 标记每个候选 `[UNVERIFIED]`，以便下游分析继续进行审计可见的降级输出，而不是默默地丢弃候选。

```bash
# 1. 通过规范的严格安全链（§2）解决 $VERIFY_PAPERS。
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
VERIFY_PAPERS=".aris/tools/verify_papers.py"
[ -f "$VERIFY_PAPERS" ] || VERIFY_PAPERS="tools/verify_papers.py"
[ -f "$VERIFY_PAPERS" ] || { [ -n "${ARIS_REPO:-}" ] && VERIFY_PAPERS="$ARIS_REPO/tools/verify_papers.py"; }
[ -f "$VERIFY_PAPERS" ] || VERIFY_PAPERS=""

# 2. 发出候选 JSON 。验证暂存于 .aris/ 下
#    （不在 research-wiki/ 下 — 第 6 步的 wiki 摄取谓词是
#    “research-wiki/ 存在”，我们不能从步骤 1.5) 中跳出它。
mkdir -p .aris/verify-papers
cat > .aris/verify-papers/candidate_papers.json <<'JSON'
[
  {"id": "p1", "arxiv_id": "2307.03172", "doi": null, "title": "Lost in the Middle"},
  {"id": "p2", "arxiv_id": null, "doi": "10.1145/...", "title": "..."},
  {"id": "p3", "arxiv_id": null, "doi": null, "title": "Some Paper Title"}
]
JSON

# 3. 运行3层验证（arXiv批量→CrossRef→Semantic Scholar模糊）。
#    策略D1：当辅助程序未解决或调用失败时，发出
#    降级的验证集标记所有内容[未验证]，因此用户
#    可以审核搜索质量。如果 python3 本身丢失，我们会阻止
#    而不是带壳的手卷JSON。
verify_ok=false
if [ -n "$VERIFY_PAPERS" ]; then
  if python3 "$VERIFY_PAPERS" \
        --input  .aris/verify-papers/candidate_papers.json \
        --output .aris/verify-papers/verified_papers.json; then
    verify_ok=true
  else
    echo "WARN: verify_papers.py invocation failed (resolved at $VERIFY_PAPERS); falling back to [UNVERIFIED] tagging." >&2
  fi
else
  echo "WARN: verify_papers.py not resolved at .aris/tools/, tools/, \$ARIS_REPO/tools/, or via ~/.aris/repo." >&2
  echo "      Fix: rerun bash tools/install_aris.sh or smart_update.sh (refreshes ~/.aris/repo), export ARIS_REPO, or copy the helper to tools/." >&2
fi
if [ "$verify_ok" = "false" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 unavailable; cannot emit fallback verified_papers.json." >&2
    echo "       Status: BLOCKED. Install python3 or restore the helper to proceed." >&2
    exit 1
  fi
  echo "      Emitting unverified candidate set with [UNVERIFIED] tags." >&2
  python3 - <<'PY'
import json
cands = json.load(open('.aris/verify-papers/candidate_papers.json'))
out = {
  'verdict': 'WARN',
  'reason_code': 'verify_papers_unavailable',
  'summary': 'verify_papers.py helper unresolved or invocation failed; all candidates tagged [UNVERIFIED] for audit visibility.',
  'papers': [dict(p, status='unverified', method='none') for p in cands],
}
with open('.aris/verify-papers/verified_papers.json', 'w') as f:
  json.dump(out, f, indent=2)
PY
fi

# 4. 从 .aris/verify-papers/verified_papers.json 读取判决+每篇论文状态；
#    向用户发出表面警告。
```

**强制输出规则**（完整合约参见[`shared-references/citation-discipline.md`](../shared-references/citation-discipline.md)§预检索验证协议）：

- 为分析列表中的每篇论文标记其状态： `✅ verified (via arxiv|crossref|s2)` 或 `⚠️ UNVERIFIED (reason)` 或 `… verify_pending` 。
- **永远不要默默地丢弃未经验证的论文** - 将它们保留在带有 `[UNVERIFIED]` 标记的输出中，以便用户可以审核搜索质量。
- 切勿凭记忆编造 DOI 或 arXiv ID。若字段未知，在 `candidate_papers.json` 中将其保留为 `null`；辅助程序会回退到按标题搜索。
- 如果辅助程序返回 `WARN` 和 `high_hallucination_rate` ，则逐字显示警告并建议使用更窄的查询重新运行。
- 对于标记为 `verify_pending` 的论文，不要将其提升为 `verified` — 向用户显示挂起状态并在下一个会话中重试。

可选：在 shell 中设置 `ARIS_VERIFY_EMAIL=you@institution.edu` 以将 CrossRef 速率限制提升到礼让池。

### 第二步：分析每篇论文

> **论文分发（分层感知）。** 每篇论文的提取是纯粹的广度——每篇论文都是独立的——因此它可以干净地并行化。 **第 1 层**（工作流程）：为每篇论文（或每小批量）生成一个 Claude 子代理以提取下面的字段。 **第 2 层**（代理工具，无工作流程）：通过代理工具使用相同的每篇论文子代理。 **第 3 层**：按顺序迭代。这遵循 [ `shared-references/fan-out-pattern.md` ](../shared-references/fan-out-pattern.md) — `{shard_id: "<paper-or-batch id>", entries: [{dedup_key: "<canonical arXiv-id / DOI / title-hash, already assigned upstream in Step 1.5>", problem, method, results, relevance, source, verification_status}]}` 的*提取*分片数据结构。
> 这里的“陪审团”**不是模型** - 它是**确定性** `verify_papers.py` 门，已经在步骤 1.5 中运行（3 层 arXiv / CrossRef / Semantic Scholar 交叉检查）。因为接受门是确定性验证器，而不是模型判决，所以自动满足跨模型族规则（进程不是模型族 - 请参阅 [ `acceptance-gate.md` ](../shared-references/acceptance-gate.md)），因此这是并行分发设计空间的**近零风险**角落。每篇论文的工作是**提取，而不是裁决**：分片逐字报告每篇论文的内容及其验证状态；他们**不**决定哪个论文“计数”（步骤1.5已经机械地做了），并且他们**不**放弃除`verified`之外的任何状态的论文。综合（步骤 3）是“解释性”聚合——按主题分组，发现我们的工作可以填补的空白——在已经承认的集合上；这是执行者的正常工作，而不是对论文是否*重要*的 accept/reject 判决。跨模型系列规则控制着准入判决，这里的准入是确定性的 Step-1.5 门，因此无需模型陪审团即可满足不变量。

对于 `.aris/verify-papers/verified_papers.json` 中的**每篇**论文（已验证、未验证、`verify_pending` 和 `error` 等 - 请参阅上面的保留规则），摘录：
- **问题**：它解决了什么差距？
- **方法**：核心技术贡献（1-2句话）
- **结果**：关键数值和论断
- **相关性**：它与我们的工作有何关系？
- **来源**：我们在哪里找到它 (Zotero/Obsidian/local/web) — 帮助用户了解他们已有的内容与新增内容
- **验证状态**（其中之一）：
  - `✅ verified (via arxiv|crossref|s2)`
  - `⚠️ UNVERIFIED (verification unavailable: helper unresolved or invocation failed)`
  - `⚠️ UNVERIFIED (searched: not found in any source)`
  - `… VERIFY_PENDING (transient API failure — retry next session)`
  - `❌ ERROR (malformed input: no arxiv, no DOI, no title)`

  在分析表中显示状态——永远不要因为论文的状态不是 `verified` 就默默地删除它。

### 第三步：合成
- approach/theme 课题组论文
- 确定该领域的共识与分歧
- 寻找我们的工作可以填补的空白
- 如果存在 Obsidian 注释，请将用户自己的见解纳入综合中

### 第四步：输出
以结构化文献表形式呈现：

```
| 论文 | 目标会议或期刊 | 方法 | 主要结果 | 与我们的相关性 | 来源 |
|-------|-------|--------|------------|-----------------|--------|
```

加上研究现状的叙述性摘要（3-5 段落）。

如果导出了 Zotero BibTeX，请包含 `references.bib` 片段以直接在论文写作中使用。

### 第 5 步：保存（如果需要）
- 将论文 PDF 保存为 `literature/` 或 `papers/`
- 更新项目内存中的相关工作笔记
- 如果 Obsidian 可用，可选择在库中创建文献综述笔记

> **组合模式** - 如果使用 `— composed: <canonical-report-path>` 调用（像 `/idea-discovery` 这样的协调器通过此），请不要**编写独立的研究现状 `.md` 。返回结构化表格+叙述性摘要，以便编排器将其折叠到其规范报告中作为“文献研究现状”部分；该报告链接任何已保存的 PDFs/ `references.bib` ，它不会获得重复的研究现状文件。第 6 步（研究维基摄取）仍在运行 - 维基是一个单独的持久存储，而不是报告的副本。**默认（无 `— composed:` 指令）：行为与上述完全相同 - 独立，按照文档写入文件。** 切勿从仅存在于磁盘上的报告文件推断组合模式。完整规则：[ `shared-references/output-composition.md` ](../shared-references/output-composition.md)。

### 第 6 步：更新研究 Wiki

**当 `research-wiki/` 存在时需要。** 如果目录不存在，则完全跳过（无操作，无错误）。根据 [ `shared-references/integration-contract.md` ](../shared-references/integration-contract.md)，此步骤遵循规范的摄取合同 - 业务逻辑位于 `tools/research_wiki.py` 中，而不是在本文中。

当 `research-wiki/` 存在时，根据 [ `shared-references/wiki-helper-resolution.md` ](../shared-references/wiki-helper-resolution.md) 中记录的规范链解析 `$WIKI_SCRIPT` （变体 B — 警告并跳过）：

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
ARIS_REPO="${ARIS_REPO:-$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null)}"
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
  ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
WIKI_SCRIPT=".aris/tools/research_wiki.py"
[ -f "$WIKI_SCRIPT" ] || WIKI_SCRIPT="tools/research_wiki.py"
[ -f "$WIKI_SCRIPT" ] || { [ -n "${ARIS_REPO:-}" ] && WIKI_SCRIPT="$ARIS_REPO/tools/research_wiki.py"; }
[ -f "$WIKI_SCRIPT" ] || {
  echo "WARN: research_wiki.py not found; literature synthesis will be reported but wiki ingest will be skipped. Fix: bash tools/install_aris.sh or smart_update.sh (refreshes ~/.aris/repo), export ARIS_REPO, or cp <ARIS-repo>/tools/research_wiki.py tools/." >&2
  WIKI_SCRIPT=""
}
```

```
📋 研究维基摄取（运行一次，在研究结束时）：
   [ ] 1. 谓词：`research-wiki/` 存在吗？如果不是，请跳过此步骤。
   [ ] 2. 如果 $WIKI_SCRIPT 为空（辅助程序无法访问），则跳过此步骤
          （上面的警告已经解释了原因）。
   [ ] 3. 对于前 8-12 篇相关论文中的每一篇（上面收集的 arxiv ID）：
          python3 " $WIKI_SCRIPT " ingest_paper research-wiki/ \
              --arxiv-id <id> [--核心论点 "<one-line>"] [--tags <t1>,<t2>]
   [ ] 4. 对于与现有 wiki 实体的每个显式关系，
          添加一条边：
          python3“$WIKI_SCRIPT”add_edge research-wiki/ \
              --from "论文:<slug>" --to "<target_node_id>" \
              --type <extends|contradicts|addresses_gap|inspired_by|...> \
              --evidence "<单句引用或推理>"
   [ ] 5.确认papers/<slug>.md文件已创建（辅助程序打印
          “论文摄入：...”）；如果因网络错误而失败，
          重试或退回到 --title/--authors/--year 手动表格。
```

`ingest_paper` 在一次调用中处理 slug 生成、arXiv 元数据获取、重复数据删除（通过 arXiv id 跳过现有论文）、页面渲染、`index.md` 重建、`query_pack.md` 重建和日志追加 — **不要手动编写 `papers/<slug>.md`**。如果辅助程序不可用（例如，在非 ARIS 机器上离线，或 `$WIKI_SCRIPT` 为空），请记录间隙并让 `/research-wiki sync --arxiv-ids …` 稍后回填。

对于非 arXiv 来源（仅限 Semantic Scholar、没有 arXiv 镜像的 IEEE/ACM 期刊、博客文章），请改为传递手动元数据：

```bash
python3 "$WIKI_SCRIPT" ingest_paper research-wiki/ \
    --title "<full title>" --authors "A, B, C" --year <yyyy> \
    --venue "<venue>" [--external-id-doi "<doi>"] [--thesis "..."]
```

## 主要规则
- 始终包含论文引文（作者、年份、目标会议或期刊）
- 区分同行评审和预印本
- 诚实地对待每篇论文的局限性
- 注意论文是否直接与我们的方法竞争或支持我们的方法
- **永远不会因为未配置 MCP 服务器而失败** — 始终优雅地回退到下一个数据源
- Zotero/Obsidian 工具可能具有不同的名称，具体取决于用户如何配置 MCP 服务器（例如， `mcp__zotero__search` 或 `mcp__zotero-mcp__search_items` ）。尝试最常见的模式并适应。

