# Wiki Agent：分类与跨论文关系

`paper-wiki-agent` 读取完整 PDF，写入一份 Paper、一份论文自身的 Idea，以及论文报告的实验方向。Idea 仅记录论文思路、证据与关联，不生成 Key risks 小节或要求 limitations 字段。当前不自动生成 Claim。笔记属于对作者工作的整理，不代表本项目已复现。

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

Experiment 不再包含 `verdict`、`confidence`、`date`、`hardware`、`duration`、`provenance` 字段。实验设计、数据、结果和页码证据保留在正文，来源论文通过关联的 Idea 查找。已有 `research_object` 标签合并到 `research_field`，去重后保留。

## 关系发现

单篇分析独立完成后，Wiki Agent 读取当前 Wiki `papers/` 下的其他论文笔记。候选不超过 8 篇时全部比较；超过 8 篇时，先把全部候选的标题、分类和简短 thesis 交给模型选择最多 8 篇，再读取选中的完整 Markdown 笔记。候选包含同问题工作与跨领域可迁移方法，不使用领域标签做硬过滤。该阶段会增加 1～2 次模型请求；没有其他论文时不增加请求。

第二阶段比较当前 Paper、当前提取的 Idea 与候选 Paper，并读取通过来源边关联到候选 Paper 的已有 Idea，允许以下关系：

| 起点 → 终点 | 类型 | 要求 |
| --- | --- | --- |
| Paper → Paper | `extends` | 扩展工作指向前作，需要明确继承依据 |
| Paper → Paper | `supersedes` | 替代工作指向被替代工作，需要明确依据 |
| Paper ↔ Paper | `contradicts` | 在可比较条件下结论冲突，不能把条件不同当成矛盾 |
| Paper ↔ Paper | `shares_method` | 共享具体方法机制，不能只是都使用机器学习 |
| Paper ↔ Paper | `addresses_same_problem` | 解决相同的明确研究问题，主题标签相同不够 |
| 当前 Idea → 其他 Paper，或已有 Idea → 当前 Paper | `inspired_by` | 有文献依据的思路来源，不能从相似性猜测历史来源 |
| 当前 Idea → 其他 Paper，或已有 Idea → 当前 Paper | `may_apply_to` | 针对目标论文问题的方法迁移假设，注明机制、条件与局限，尚未验证 |

同篇的 Idea → Paper `inspired_by` 和 Idea → Experiment `tested_by` 继续写入。不生成 Idea → Idea 或 Claim 相关关系。

每条跨论文关系必须提供双方笔记正文中的逐字摘录和解释，代码检查节点范围、实体组合、关系类型、非空理由及摘录是否真实存在。保存的 `evidence` 包含双方笔记路径、摘录、解释；迁移关系另有明确的未验证标记。无充分依据允许零条关系。逐字校验并不能保证推理科学上正确，语义判断仍由模型完成；比较依据是已保存的论文笔记，不会在关系阶段重新读取所有旧 PDF。

对称关系按节点 ID 排序保存，方向没有科学含义。扩展、替代和来源等有向关系保留语义方向。

## 重读与展示

自动推断的跨论文边带 `origin: wiki-agent-relations-v1` 和 `owner: paper:<slug>`。成功重新分析关系后，原子替换本篇负责的自动关系；保留手工边和其他论文负责的关系。所有模型关系先通过校验再修改关系文件，失败不会提交一部分新关系。关系阶段失败会使本次导入报错，但此前已写入的单篇笔记可能存在，重试会继续复用它们。每次成功导入重建索引与 query pack，后者包含近期关系摘要。

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

## 图片参考检索工具

`search_figure_references(query="编码器 解码器 注意力架构", top_k=3)` 独立检索共享 Wiki 的 `figures/<paper-slug>/`，根目录沿用 `PAPER_AGENT_WIKI_ROOT`。工具已提供给 `paper_figure` 阶段，不修改绘图 skill 或自动触发生图。

结果包含 `image_path`、`caption_path`、完整 `caption` / `caption_markdown`、`paper_id`、`pdf_page`、相关性分数和匹配词。默认返回最多 3 条，`top_k` 范围 1–20；不匹配返回空列表。仅检索同名图片存在的 Markdown 图注，忽略孤立文件及指向库外的符号链接。

排序使用图注 BM25 加常见设计术语的中英映射（如编码器/encoder、解码器/decoder、多专家/experts）。这是本地文本相关性检索，不是向量语义检索或视觉布局检索；查询最好包含具体任务、模块和表达需求。工具不发送图片给模型、不生成 prompt、不调用生图接口；图片路径留给后续看图流程使用。
