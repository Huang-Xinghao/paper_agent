# AI 会议电池论文：现有 PDF 缺口调研

对照现有 419 篇 PDF，找到 10 篇未匹配到本地 PDF 的候选：5 篇优先、1 篇诊断系统方向、4 篇控制/调度扩展方向。后续下载结果：6 篇已下载，4 篇因来源访问受限暂未下载；见 [下载清单](../research-wiki/raw-paper/battery-ai-conference-papers.md)。检索面向 2024–2026 年。

## 最值得补充

### 优先：状态、寿命与故障

- **[Battery Fault: A Comprehensive Dataset and Benchmark for Battery Fault Diagnosis](https://proceedings.iclr.cc/paper_files/paper/2026/hash/0d781fa5f639bf2caf728a68e9678362-Abstract-Conference.html)** — ICLR 2026。车载真实运行数据结合机理约束生成的电池故障诊断数据集 CH-BatteryGen，适合故障方向；并非纯实测数据集。 核验状态：官方论文页面和出版信息已核实。
- **[LiPM: Foundation Model for Lithium-Ion Battery Analysis](https://doi.org/10.1145/3711896.3737027)** — KDD 2025。自监督电池基础模型，联合掩码重建、库仑积分约束和不规则序列编码，直接对应局部充放电曲线。 核验状态：索引元数据已确认；出版社页面本次访问受限。
- **[Physics-Informed Multi-Task Learning for Battery State of Health Prediction with Uncertainty Quantification](https://doi.org/10.1609/aaai.v40i2.37143)** — AAAI 2026。PINN 与深度自编码高斯混合模型联合学习 SOH 和可靠性代理分数；代理分数不等同于有覆盖保证的预测区间。 核验状态：索引元数据已确认；出版社页面本次访问受限。
- **[RUL-QMoE: Multiple Non-crossing Quantile Mixture-of-Experts for Probabilistic Remaining Useful Life Predictions of Varying Battery Materials](https://doi.org/10.1609/aaai.v40i47.41472)** — AAAI proceedings 2026。不同材料的专家网络与非交叉分位数回归，适合与 iMOE 比较，并研究寿命预测不确定性。 核验状态：索引元数据已确认；出版社页面本次访问受限。
- **[TA-TFN: Task-Adaptive Frequency Decoupling for Generalizable Battery State Estimation across Chemistries](https://doi.org/10.1145/3770855.3818162)** — KDD 2026。任务自适应时频分解，联合 SOC/SOH 估计，重点研究跨化学体系泛化。 核验状态：索引元数据已确认；出版社页面本次访问受限。

### 诊断系统

- **[LiBrain: LLM-Powered Li-ion Battery Diagnostics with Time-Series-Aware Retrieval-Augmented Framework for E-bikes](https://ojs.aaai.org/index.php/AAAI/article/view/41439)** — AAAI proceedings; also AI Magazine 2026。时序诊断与检索增强的大模型系统，适合电池诊断助手；与纯电芯退化回归有所不同。 核验状态：索引元数据已确认；出版社页面本次访问受限。

### 控制与调度扩展

- **[Efficient Cost-Minimization Schemes for Electrical Energy Demand Satisfaction by Prosumers in Microgrids with Battery Storage Capabilities](https://ijcai.org/proceedings/2024/207)** — IJCAI 2024。微电网含储能的用能成本优化，偏调度与优化。 核验状态：官方论文页面和出版信息已核实。
- **[Enabling Delayed-Full Charging Through Transformer-Based Real-Time-to-Departure Modeling for EV Battery Longevity](https://doi.org/10.1609/aaai.v40i45.41228)** — AAAI 2026。以离开时间预测支持延迟充满，偏充电策略与寿命管理。 核验状态：索引元数据已确认；出版社页面本次访问受限。
- **[Optimize Battery Control: A Multi-Objective Evolutionary Ensemble Reinforcement Learning Approach](https://ijcai.org/proceedings/2025/1024)** — IJCAI 2025。多目标进化集成强化学习的电池控制，偏控制策略。 核验状态：官方论文页面和出版信息已核实。
- **[Optimizing the Battery-Swapping Problem in Urban E-Bike Systems with Reinforcement Learning](https://ijcai.org/proceedings/2025/1029)** — IJCAI 2025。电动自行车换电系统的强化学习调度，偏运营优化。 核验状态：官方论文页面和出版信息已核实。

## 已有，排除重复

- BatteryLife: A Comprehensive Dataset and Benchmark for Battery Life Prediction：`RUL_031_2025_KDD_BatteryLife-A-Comprehensive-Dataset-and-Benchmark-for-Battery.pdf`。
- BatteryMFormer: Multi-level Learning for Battery Degradation Trajectory Forecasting：`Trajectory_009_2026_arXiv_BatteryMFormer-Multi-level-Learning-for-Battery-Degradation.pdf`。
- Battery-Sim-Agent: Leveraging LLM-Agent for Inverse Battery Parameter Estimation：`BatteryModeling_001_2026_arXiv_Battery-Sim-Agent-Leveraging-LLM-Agent-for-Inverse-Battery.pdf`。
- BatteryML: An Open-source Platform for Machine Learning on Battery Degradation：`BatteryModeling_004_2023_arXiv_BatteryML-An-Open-source-platform-for-Machine-Learning-on-Battery.pdf`。

BatteryMFormer、Battery-Sim-Agent 在现有登记中仍标为 arXiv；检索索引列出了 KDD 2026 出版记录。这是版本/出版信息更新线索，不是新的论文；本次未改名。BatteryML 同样已有预印本 PDF，不因会议发表重复计数。

## 检索与核验边界

已查官方目录：ICML 2024/2025；ICLR 2024/2025/2026；NeurIPS 2024；IJCAI 2024/2025。AAAI、KDD 使用 OpenAlex 的会议来源过滤、题名检索及出版 DOI/摘要元数据补充；部分 ACM 和 AAAI 官方页面返回 403/520，不能称已通读或完全核实全文。AAAI 会议论文集中可能包含应用会议或其他专题轨道，本文未将全部条目认定为主会技术论文。LiBrain 同时有 AI Magazine 和 AAAI proceedings 记录，按同一工作计一次。

NeurIPS 2025 官方索引本次未提供可用论文条目；ICML 2026 的具体论文集卷号未核实，未计入已查范围。CIKM 关键词检索本轮未得到可确认的直接电池候选。这是一轮定向检索，不是对所有会议、年份或摘要的穷尽扫描。排除仅涉及设备供电、量子电池学生摘要等与锂离子电池数据研究不直接相关的条目。

排重采用命名登记表的规范化标题，以及现有 PDF 首页全文匹配；因此“未收录”表示本轮未发现匹配，并非对所有不同标题版本的数学保证。文献来源与摘要快照见同名 JSON。
