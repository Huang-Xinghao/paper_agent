# Method 作者核查说明

| 正文部分 | 主要依据 |
| --- | --- |
| 外部文献 | `docs/wiki.md`；`paper_agent/agent/wiki.py`；`paper_agent/agent/wiki_relations.py`；`paper_agent/tools/literature.py`；`paper_agent/tools/paper_figures.py` |
| 双库 | `电池资源库/资源库总览.md`；`paper_agent/tools/battery_resources.py`；`paper_agent/tools/wiki.py`；`paper_agent/tools/dataset_inspection.py` |
| 工作流 | `paper_agent/agent/workflow.py`；`paper_agent/agent/loop.py`；`paper_agent/agent/experiment_executor.py`；`paper_agent/tools/profiles.py` |
| 人机协作 | `docs/runtime.md`；`paper_agent/agent/workflow.py` 的检查点与恢复逻辑 |
| Benchmark | `/home/xinghaohuang/battery-autoresearch-bench/README.md`；`tasks/matr-rul100-v1/`；`evaluation/README.md`；`evaluation/paper.py`；`evaluation/figures.py`；`evaluation/SOURCES.md` |
| 图参考 | `paper_agent/tools/figure_grouping.py`；`paper_agent/tools/figure_references.py`；`docs/wiki.md` |
| 外部评审 | `docs/external-review.md`；`paper_agent/tools/review.py`；`paper_agent/agent/review_runner.py`；`paper_agent/agent/workflow.py` 的 improvement 节点 |

### 正式投稿前需落实的信息

1. **生成模型与评测模型分开报告。** 主工作流、思路/代码/论文评审、图片生成、论文评分、图像评分可能使用不同模型；应记录实际模型标识、参数和软件版本，不能只引用代码默认值。
2. **明确 benchmark 的知识库条件。** 当前 benchmark README 说明 Paper-Agent 镜像不复制宿主机研究 Wiki，生成的 knowledge 目录仅登记本次数据，也未打包完整电池资源库。因此，当前配置不能被称为“完整双库版 Paper-Agent”，也不能用于直接证明双库收益。若论文主张依赖双库，需显式配置并记录允许访问的库快照，再进行相应对照。
3. **计划与完成分开。** 两份 campaign 配置分别列出 Paper-Agent/AI-Scientist-v3 和 Codex/Claude Code，每个系统计划 3 次运行；campaign 不自动调度，独立运行也不表示统一了底层随机种子。应根据实际运行记录填写样本量。
4. **已有评分不作为当前 MATR 跨系统结果。** `evaluation/runs/demo-project15-vs22-20261028.md` 记录的是两个既有 Paper-Agent 稿件的文本评分，实际裁判为 `gpt-6.1-sol`；它们不是四系统 MATR 对照，也不是原协议默认裁判的复现。
5. **实验性能与论文分数分开。** 当前平台没有密封测试集，论文审稿不复跑实验。若增加统一预测性能结论，需要另行定义共同划分与评价器；如果仅报告论文质量，应保留现有范围说明。
6. **图片评测需要匹配内容的参考图。** 其他文章的风格借鉴图与当前方法的人工作图参照用途不同。没有匹配参考图时，不应声称已完成 PaperBanana 有参考图评分。
7. **论文引用需补齐。** MinerU、实际使用的数据源与上游方法、AI Scientist 审稿协议及 PaperBanana 均应在正式稿中引用对应来源。评测来源链接和固定上游版本见 benchmark 的 `evaluation/SOURCES.md`。本稿未编造 BibTeX 或未核实的文献编号。
