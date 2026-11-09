# Wiki Agent：分类与跨论文关系

`paper-wiki-agent` 读取完整 PDF，写入一份 Paper、一份论文自身的 Idea，以及论文报告的实验方向。Idea 仅记录论文思路、证据与关联，不生成 Key risks 小节或要求 limitations 字段。当前不自动生成 Claim。笔记属于对作者工作的整理，不代表本项目已复现。

## 提示词维护入口

所有 Wiki 模型提示词统一维护在 [`paper_agent/agent/wiki_prompts.py`](../paper_agent/agent/wiki_prompts.py)，当前版本为 `wiki-prompts-v8`。提示词指令及示例说明统一使用英文，包括 Wiki 取证任务和动态预算提醒；分类仍要求规范中文标签，取证报告仍用中文，来源摘录保持原文。其他模块只导入和调用，不再各自拼接规则。

| 阶段 | 入口 | 用途 |
| --- | --- | --- |
| PDF 入库 | `GENERATION_PROMPT` | 共享规则、完整字段要求、原名映射与书目提取 |
| 独立审核 | `REVIEW_PROMPT` | 共享内容规则、来源覆盖检查、审核专用 JSON；不要求重写草稿 |
| 一次定向修订 | `REVISION_PROMPT` | 修订正文、证据状态、关键主张、Idea、数据集与实验；不重新提取书目或修改论文身份 |
| 关系候选筛选 | `RELATION_SELECTION_PROMPT` | 选取可比较的已有论文 |
| 跨论文关系 | `RELATION_PROMPT` | 用两端原文摘录支持关系 |
| 查询已有 Wiki | `RESEARCH_PROMPT`、`research_budget_prompt` | 有预算的文献取证，与 PDF 入库分开运行 |

共享规则定义一次后按阶段组合；独立模型请求各自携带所需规则。审核只引用共同内容标准，不携带生成阶段的完整字段提取指令。`Connections` 由程序生成，不要求模型在草稿中生成。字段校验、逐字证据检查和发布门槛仍由程序执行。修改模型规则时同步升级 `PROMPT_VERSION`，使 Zotero 分析缓存识别更新；不批量改写已有笔记。

## 入库写入与重分析

入库先检查不可修订的论文身份、论文分类和书目提取，再执行正文、实体结构、数据集目录匹配、实体分类及证据预检；预检通过后才发起独立审核。可修订问题复用现有最多一次修订与复审流程，修订请求同时携带数据集目录和已有分类词表。提示词正文未因这次流程调整而修改。

同一论文重新分析时，会更新同名实验的 `Question`、`Hypothesis`、`Protocol`、`Results`，保留额外章节和已有元数据；分类仍由本轮分析独立更新。标准正文小节内的人工修改会被本轮来源分析替换，人工备注应放在额外章节。最终落盘检查包括实验正文，避免只有文件存在却仍是旧结果。

Paper、Idea 和 Experiment 写入期间延迟刷新索引，成功入库后统一重建一次 `index.md`。底层工具及 CLI 默认仍在独立写入后刷新索引。全库书目继续在新论文到达后重新匹配，但各论文的引用边合并为一次图提交，保留手工边和其他来源边。已有确认元数据直接传给入库工具，避免再次请求 arXiv。草稿来源和最终发布质量报告分别只保存一次，审核失败诊断继续保存在草稿目录。

不再生成 `query_pack.md`；相关生成函数、CLI 命令和可视化入口均已删除。研究上下文通过现有 Wiki 检索流程按需读取。

## Paper 正文结构

后续写入的 Paper 使用 `Abstract`、`One-line thesis`、`Problem / Gap`、`Method`、`Dataset and Features`、`Limitations / Failure Modes`，并保留 `Connections`。其中 `Abstract`、`One-line thesis`、`Dataset and Features` 必填。不再写入 `Key Results` 和 `Experiments` 小节；论文实验仍作为独立 Experiment 节点保存。

`## Abstract` 保存论文完整原文摘要，保留原语言、每个句子、数字及作者自命名，只整理 PDF 断行，不缩写、翻译或通用化改写，也不在摘要内部插入页码证据。PDF 没有摘要时明确写 `Not available in the source PDF.`。章节纠正请求复用已经解析的原文页，供模型恢复完整摘要。

重新分析某篇旧论文时，写入器删除该页的 `Key Results`、`Experiments` 及旧 `Abstract (original)`，统一写入 `Abstract`；不会批量迁移其他现有笔记。分析签名已更新，之后同步可识别新版结构。

## Idea 的结构描述

WikiAgent 的 Idea 提取包含必填 `title`、`thesis`、`structure`，不生成独立的 `## Evidence` 章节或 `idea.evidence` 字段。`Thesis` 说明具体问题、研究缺口、核心假设、机制为何有效及其适用条件。新增正文标题按用户指定拼写为 `## strcture`，对应模型 JSON 的 `idea.structure` 和写入器/CLI 的 `structure` / `--structure`。

`strcture` 开头必须先画分步流程图或 ASCII 图，使用 fenced `text` 代码块，紧接在 `## strcture` 下方。图中展示输入 → 处理模块 → 输出，以及适用的分支、融合、路由和条件输入，标注有依据的形状或格式；不能只写“[分步流程或 ASCII 图]”占位。非神经方法也画其实际工作流程。

图后描述输入和输出的语义、张量形状、各轴含义（如 batch、时间长度、特征数、预测长度），以及论文报告的单位或数据类型；随后按处理顺序说明模块作用、连接关系、中间表示、形状变化、融合/路由/条件信息和输出头，必要时区分训练与推理。维度、轴顺序、层数和隐藏宽度没有来源依据时省略，不填 Unknown 等占位，也不猜数值；非张量或非神经方法写实际输入输出格式和流程，并标明不适用项。

Paper、Idea、Experiment 的生成正文及数据集/证据描述不再要求附 PDF 页码，也不添加 `(PDF pp. …)`、`[p. …]` 等位置标记。Idea 的支持观察融入 Thesis 与 strcture，不单独设 Evidence 章节。初次分析、章节纠正和实验筛选使用同一规则；完整原文摘要不做改写。参考文献摘录及内部 `references.pages` 页码仍用于书目核验和 `cites` 关系，不属于正文引用标注。

之后重新分析同一篇论文时，会更新其 `<paper-slug>-paper-idea` 节点，防止旧的 slug 去重逻辑跳过新结构。其他已有节点不做批量迁移。分析签名同步升级。

## 通用机制描述与书目引用

之后导入或重新分析论文时，Paper 的分析正文（完整原文 `Abstract` 除外）、Idea 和 Experiment 的标题与正文，以及分类标签，使用通用的具体机制描述，不使用该论文为方法、框架或模块新造的名称和缩写。描述应保留输入、操作、输出、组件作用及适用条件，不能仅改成“某模块”。已有通用方法名、数据集身份和既有基线名称可以保留。Paper 原始书目标题、稳定 node_id、原文摘要、参考文献原文及原始图注保留来源信息。

模型同时返回 `coined_terms: [{name, description}]` 与 `references` 数组，二者为必填；没有对应内容时返回 `[]`。程序在写笔记前检查已声明的自命名是否残留在知识描述中，发现后报错。该检查依赖模型完整列出名称，不能保证发现漏报的自命名。原名与通用描述映射仅存放在来源记录 `papers/<slug>.references.json`，不作为新的知识节点。

每条参考文献保存 `label`、原始 `title`、`authors`、`year`、`doi`、`arxiv_id`、`pages` 和 `quote`；缺失的标识不能猜测。程序检查摘录确实存在于指定的连续 PDF 页中（允许空白差异），并检查书目信息和标识由摘录支持。参考文献提取完整性及其是否属于参考文献列表仍依赖模型；摘录校验不构成书目内容的独立核实。

引用边方向为 `引用论文 → 被引论文`，类型为 `cites`，与模型推断的语义边独立。优先按 DOI / arXiv ID 匹配；没有标识匹配时，只接受规范化标题、年份与作者姓氏共同匹配。标识冲突或多个候选时不连边。引用只表示书目事实，不代表扩展、支持或方法继承。

没有入库的被引论文仅保留书目记录，不创建空节点。每次导入后重新解析已保存的参考文献，允许旧记录匹配新入库论文；不会重读或重写旧论文笔记。自动引用边带 `origin: wiki-agent-citations-v1`，按来源论文刷新，保留手工边及语义边。`citation_count` 单独统计本篇已匹配的出向引用。

Zotero 分析签名已升级，并将参考文献来源文件列为必需输出；下次同步时旧签名会触发重新分析。本次代码更新没有批量重读或改写现有 18 篇笔记，也未改变检索流程。

## 节点分类

Paper、Idea、Experiment 分别分类，五个字段均为必填的多选字符串数组，不涉及的维度使用 `[]`。不接受旧版只含 domain/task 的分类结构。

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `research_field` | 学科、应用领域与研究对象（合并） | 电池、机器学习、电芯、电池包、电解液、通用时间序列 |
| `task` | 研究问题 | soh、rul、degradation trajectory、时间序列预测、电导率预测 |
| `method` | 使用的方法 | 图神经网络、Transformer、混合专家、分子动力学 |
| `data_type` | 数据表示 | 时间序列、图、分子结构、表格 |
| `scenario` | 应用与测试场景 | 实验室、实车、储能、退役电池 |

这些字段是开放词表。模型获得已有标签作为命名参考，应复用同义标签，但分类证据只能来自当前论文。背景提及或计划中的跨领域应用不能作为论文已研究该对象的依据。电芯数据集目录为 `电池资源库/电芯/数据集/`。WikiAgent 统一使用 MinerU 读取完整 PDF，按原始 PDF 页码组织正文、公式、表格和图注。

Experiment 不再包含 `verdict`、`confidence`、`date`、`hardware`、`duration`、`provenance` 字段。实验问题、假设、方案、结果和支持性观察保留在正文，不附 PDF 页码标注；来源论文通过关联的 Idea 查找。已有 `research_object` 标签合并到 `research_field`，去重后保留。

## 关系发现

单篇分析独立完成后，Wiki Agent 读取当前 Wiki `papers/` 下的其他论文笔记。候选不超过 8 篇时全部比较；超过 8 篇时，先把全部候选的标题、分类和简短 thesis 交给模型选择最多 8 篇，再读取选中的完整 Markdown 笔记。候选包含同问题工作与跨领域共享机制或有明确来源依据的工作，不使用领域标签做硬过滤。该阶段会增加 1～2 次模型请求；没有其他论文时不增加请求。

第二阶段比较当前 Paper、当前提取的 Idea 与候选 Paper，并读取通过来源边关联到候选 Paper 的已有 Idea，允许以下关系：

| 起点 → 终点 | 类型 | 要求 |
| --- | --- | --- |
| Paper → Paper | `extends` | 扩展工作指向前作，需要明确继承依据 |
| Paper → Paper | `supersedes` | 替代工作指向被替代工作，需要明确依据 |
| Paper ↔ Paper | `contradicts` | 在可比较条件下结论冲突，不能把条件不同当成矛盾 |
| Paper ↔ Paper | `shares_method` | 共享具体方法机制，不能只是都使用机器学习 |
| Paper ↔ Paper | `addresses_same_problem` | 解决相同的明确研究问题，主题标签相同不够 |
| 当前 Idea → 其他 Paper，或已有 Idea → 当前 Paper | `inspired_by` | 有文献依据的思路来源，不能从相似性猜测历史来源 |

同篇的 Idea → Paper `inspired_by` 和 Idea → Experiment `tested_by` 继续写入。不生成 Idea → Idea、Claim 相关关系或假设性方法迁移关系。

每条跨论文语义关系必须提供双方笔记正文中的逐字摘录和解释，代码检查节点范围、实体组合、关系类型、非空理由及摘录是否真实存在。保存的 `evidence` 包含双方笔记路径、摘录、解释。无充分依据允许零条关系。逐字校验并不能保证推理科学上正确，语义判断仍由模型完成；比较依据是已保存的论文笔记，不会在关系阶段重新读取所有旧 PDF。`cites` 不由该阶段生成，不受最多 8 篇语义候选限制，证据为来源参考文献原文和 PDF 页码。

对称关系按节点 ID 排序保存，方向没有科学含义。扩展、替代和来源等有向关系保留语义方向。

## 重读与展示

自动推断的跨论文边带 `origin: wiki-agent-relations-v1` 和 `owner: paper:<slug>`。成功重新分析关系后，原子替换本篇负责的自动关系；保留手工边和其他论文负责的关系。所有模型关系先通过校验再修改关系文件，失败不会提交一部分新关系。关系阶段失败会使本次导入报错，但此前已写入的单篇笔记可能存在，重试会继续复用它们。每次成功导入重建索引，关系由图文件和现有检索流程读取。

旧 Wiki 已清空；新条目按当前五维结构重新分析。Zotero 同步记录用于避免重复处理相同来源和分析设置。

重新生成浏览页面后，可按五个维度筛选，在节点详情中查看关系及证据：

```bash
python scripts/visualize_wiki.py research-wiki
```

页面是静态快照；导入新论文后需要重新生成。

## 架构图与原始图注

WikiAgent 通过独立 MinerU 4.x 环境一次解析完整 PDF，同时获得按页全文、公式、表格、图片块和图注。全文按 MinerU 的块顺序交给总结模型；保留嵌套图注、脚注及空白页页码。用同一份解析结果，用图注和坐标合并多子图，再根据图注的 architecture、workflow、schematic、overall framework 等关键词筛选架构图候选。不调用视觉模型，不生成看图解读，也不默认把 Figure 1 当作架构图。

设置 `PAPER_AGENT_MINERU_PYTHON=/path/to/mineru-venv/bin/python` 和 `MINERU_HOME=/path/to/mineru/home`；也可将该环境的 `mineru-kit` 放到 PATH。MinerU 是 WikiAgent 的必需运行环境。未配置、解析失败、全文输出为空或页码不完整时导入报错；不回退到 pypdf / pdftotext，也不以仅文本模式继续。解析超时由 `PAPER_AGENT_MINERU_TIMEOUT` 控制，默认 1200 秒。

架构图作为独立绘图参考库，保存到 `research-wiki/figures/<slug>/`，其中 `<slug>` 是论文 `node_id` 中 `paper:` 后面的标识，例如 `figgener2024_multiyear_field_measurements`。每张图保存同名 PNG 和 Markdown 图注，如 `page_3_figure_1.png`、`page_3_figure_1.md`；Markdown 保留原始图注、来源 node_id、PDF 页码及必要的复核标记。不写入 Paper 正文，不创建节点或关系。单篇有多张架构图时保存多组文件；没有可裁剪的候选图时目录为空。

原始结构化结果及完整分组结果缓存在 `.paper-agent/pdf-figures/`，按 PDF SHA256、MinerU 版本和处理规则区分。Zotero 的同步输出校验包含图片和图注文件。旧同步记录在首次使用新流程时会重新处理。

筛选是图注规则，可能漏掉未在图注中明确说明的架构图，也可能选中其他示意图。未匹配会明确记录；布局歧义、上置图注和跨页图保留复核标记。当前分组方法已在 iMOE 论文验证，其他出版布局仍需检查；原始图注保留解析文本，不保证 OCR 或断词完全正确。

### 补提取正文图片与归档原始 PDF

2026-11-04 按用户要求，对 battery scientist 的 18 篇论文额外补提取了全部正文编号图，不再只发布架构图关键词命中的图片。此批次共保存 94 张编号图和 1 张无编号图形摘要；图片仍使用 `figures/<slug>/` 下的 PNG 与 Markdown 图注配对保存。`figures/README.md` 提供论文目录，每篇目录中的 `README.md` 可预览图片。

可用 `python scripts/backfill_wiki_assets.py --project /path/to/paper-agent` 重做此项资产补提取。脚本复用 MinerU 的结构化缓存，修复粗体图注、远距离图注和可确认的跨页图注分组；跨页或不确定布局保留复核标记。附录、补充材料与 S 编号图不纳入此批次。该脚本不会重新生成论文笔记，也不改变 WikiAgent 默认的架构图筛选规则。

原始下载文件归档到 `research-wiki/raw-paper/<slug>.pdf`，`raw-paper/manifest.json` 记录 Zotero 条目、SHA256、原始页数和图片提取范围。原始文件若合并了补充材料，归档时保留完整文件，但图片只从正文提取；例如 ProtoMI 归档原始 79 页 PDF，分析和图片范围保持第 1–38 页。同步记录包含新增图片、图注及归档 PDF，详细结果见 `figure-backfill-report.json`。

## 图片参考检索工具

`search_figure_references(query="编码器 解码器 注意力架构", top_k=3)` 独立检索共享 Wiki 的 `figures/<paper-slug>/`，根目录沿用 `PAPER_AGENT_WIKI_ROOT`。工具已提供给 `paper_figure` 阶段，不修改绘图 skill 或自动触发生图。

结果包含 `image_path`、`caption_path`、完整 `caption` / `caption_markdown`、`paper_id`、`pdf_page`、相关性分数和匹配词。默认返回最多 3 条，`top_k` 范围 1–20；不匹配返回空列表。仅检索同名图片存在的 Markdown 图注，忽略孤立文件及指向库外的符号链接。

排序使用图注 BM25 加常见设计术语的中英映射（如编码器/encoder、解码器/decoder、多专家/experts）。这是本地文本相关性检索，不是向量语义检索或视觉布局检索；查询最好包含具体任务、模块和表达需求。工具不发送图片给模型、不生成 prompt、不调用生图接口；图片路径留给后续看图流程使用。

2026-11-04 用户随后要求精简图片库：助手直接逐张查看现有图片与图注，删除 71 张结果图、数据分布和性能对比图，保留 24 张方法架构、模型结构和流程图（含 1 张展示方法的图形摘要）。本次未调用 WikiAgent。当前清单以 `figure-curation-report.json` 和 `figures/README.md` 为准；18 篇原始 PDF 保持完整。全量补提取报告仅作为历史记录保留，重新运行补提取脚本会恢复被筛掉的图片。

### Experiment 正文与选择规则

后续 WikiAgent 每篇最多选择 3 个最重要的已报告实验，可以少于 3 个，没有实证实验时为 0 个。优先选择直接验证核心假设、关键对照/消融、揭示泛化或失败边界的实验；按科学重要性选择，不按图表顺序凑数，不把不相关问题合并。

Experiment 正文仅含 `Question`、`Hypothesis`、`Protocol`、`Results` 四节。Question 是具体可检验的问题；Hypothesis 是被检验的预期及机制，推断的假设需明确标注；Protocol 包含数据、划分单位、预处理、训练/测试、对照与控制变量、指标和重复次数；Results 包含带条件的关键数值、支持性观察、负面结果和结论边界。未报告的细节省略，不填 Unknown 等占位，也不补造实验或结果。来源与关联继续通过图关系和元数据保存，不额外添加正文小节。现有节点不批量改写。


## 本地检索与证据核验

研究节点（文献、Idea、研究精炼）先执行独立、有限预算的 Wiki 取证，再把证据包交给原阶段 Agent 撰写报告。默认只检索本地 Wiki：中文双字与英文 BM25、按来源论文合并命中、按栏目/行号读取，以及单跳关系扩展。Idea 和 Experiment 用于导航，研究主张的引文须回到 Paper 正文。相似度和图关系不代表支持结论。

每次取证最多 3 次模型请求、6 次 Wiki 工具调用（含初始候选）和 1 次图扩展，单次输出最多 4000 token；工具材料单次最多 12000、累计最多 36000 JSON 字符。报告撰写不计入此子流程预算。交接期间移除 Wiki 检索工具，避免再次开启搜索。来源缺失、请求失败或预算耗尽时明确返回证据缺口。

Agent 仅使用一个 Wiki 工具 `search_wiki(query=研究问题)`。工具内部完成按相关性搜索、读取方法/数据/限制等栏目摘录，以及可选的一跳关系扩展；已删除独立的 browse_wiki、read_wiki Python 接口，expand_wiki_relations 仍是内部实现。搜索支持 `strategy=auto|lexical|hybrid`，`include_relations=false` 可跳过关系扩展；运行器保证每轮取证最多扩展一次。所有 purpose（包括 overview）均按 query 相关性排序，不再先取文件顺序靠前的卡片。工作流预读仅支持 literature、ideas、novelty、research_refine，统一走有预算的取证入口；其他阶段调用预读会明确报错。

结果同时包含匹配片段和有限数量的论文卡片。卡片是当前 One-line thesis、Problem / Gap、Method、Dataset and Features、Limitations / Failure Modes 原文摘录，每栏目最多700字符，附栏目位置、正文指纹、质量状态和截断标记；并非重新生成的摘要，也不是另一种工具或 Wiki 节点。Agent 需要补证时继续调用同一个 search_wiki，改写研究问题、关键词或论文标题。引文仍必须存在于本轮实际交付过的同一 Paper 栏目，最终复查正文版本。内部精确读取与分页函数保留给运行器和测试，不注册为模型工具。

每轮记录位于项目 `.paper-agent/wiki-retrieval/<run>/`，含 `packet.json`、`report.md`、对话、已见摘录与调用轨迹。方案画布修订会复查最近证据包；正文变化后撤下失效引文、旧摘要和采用建议。用户改变任务条件时，仍须重新判断适用性。引文检查验证文本与版本；研究结论仍是模型解释，不代表实验复现。

新论文入库时，MinerU 结果保留为带物理页码、块 ID、坐标和原始文本的结构化来源。只保守过滤页眉页脚，保留表格、公式、图注与脚注；解析异常保留诊断，不凭空补 OCR。生成草稿后先核对逐字证据，再以独立请求审查支持关系与遗漏，最多修订一次并重审。事实支持核验仍未通过时停止发布，草稿与失败报告保留在 `.paper-agent/wiki-preprocessing/<document_id>/`。原文矛盾、报告不足和解析问题分别记录，不因改写草稿自动消失。

公开 Wiki 继续使用当前本地格式：原文完整 Abstract；Idea 的 Thesis、`strcture`（以流程/ASCII 图开头）、Connections；最多 3 个论文实际实验及其 Question/Hypothesis/Protocol/Results。不增加 Idea Evidence、状态前缀、页码定位或缺失信息占位。内部 `critical_claims` 仅用于审查，不生成 Claim 节点；参考文献提取和 `cites` 关系保留。

来源保存在 `research-wiki/sources/<document_id>.json`；Paper 旁的 `.source.json` 记录来源、PDF 指纹与侧边文件位置，`.quality.json` 记录核验结果、问题、修订和正文指纹；`.references.json` 继续单独保存原文参考文献。检索会附带质量状态，未经重读的旧页面不会自动变成已核验。正文被手工修改后，原质量报告标为 stale。

可选的本地语义检索通过 `scripts/setup_wiki_embedding.py` 准备固定版本 multilingual E5 模型，再配置 `PAPER_AGENT_EMBEDDING_DIR` 和 `PAPER_AGENT_EMBEDDING_PYTHON`。编码子进程需安装 onnxruntime、tokenizers、numpy；准备脚本还需 huggingface_hub。运行时不下载模型；`auto` 在语义不可用时明确降级为 BM25，`hybrid` 则返回错误。向量缓存按模型与正文内容寻址，可用 `PAPER_AGENT_EMBEDDING_CACHE` 指定目录。编码 worker 位于源码 scripts 目录，运行时须保留该脚本。
