---
name: paper-write
description: 根据论文规划、真实实验结果、图表和核实文献生成中文或指定语言的 LaTeX 论文源文件。
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, search_papers, search_wiki, read_wiki
---

# 论文写作

读取 `PAPER_PLAN.md`、`refine-logs/FINAL_PROPOSAL.md`、`refine-logs/EXPERIMENT_PLAN.md`、`results/`、`figures/figure_captions.md`、`figures/latex_includes.tex` 和现有论文源文件，按计划写入 `paper/main.tex`、`paper/sections/*.tex`、`paper/references.bib`。保持章节、主张、图表和证据之间的对应关系。

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