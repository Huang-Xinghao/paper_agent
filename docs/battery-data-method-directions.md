# 电池曲线与寿命数据：建议补充的方法类别

## 调研范围与结论

本次依据项目 BatteryLife 数据读取代码、MATR 目录说明、iMOE wiki，以及 11 篇已在线核实的 2024–2025 年 ICML / ICLR / NeurIPS 官方论文摘要。以下适配判断是针对本项目的研究建议，不代表这些通用方法已在电池数据上取得经验证的优势。本次未下载额外 PDF，也未导入 wiki。

优先补充：自监督表征；不规则、局部曲线与函数数据；小样本表格回归；不确定性与共形预测。若有未达到寿命阈值就停止记录的电芯，再优先补充生存分析。领域适应、领域泛化、GNN、时序预测、算子学习和 PINN 已有专门论文批次，本轮重点是补足它们没有直接解决的问题。

## 数据为什么适合这些方向

- 电芯层：材料、额定容量、实验批次、充放电协议与温度条件。
- 循环内：电压、电流、容量、温度随时间变化的曲线；不同数据集的通道、采样率和可观测区间可能不同，需要逐集核验。iMOE 特别使用局部充电容量—电压曲线与静置信号。
- 循环间：容量/SOH 演变、工况变化、拐点及最终寿命。
- 当前 MATR 目录为 169 只电芯、81 种充电协议、固定 30°C，不能单靠该数据集评估跨温度泛化。
- 对寿命任务，电芯是主要独立抽样单位。同一电芯的循环并非独立电芯样本，训练/测试划分与不确定性校准均应考虑这种依赖。

## 1. 自监督与层次表征学习：优先级高

用未标注或无完整寿命标签的曲线预训练，再微调 SOH、RUL 或退化模式。可设计循环内曲线编码器和循环间退化编码器；目标是学习可迁移的状态特征，不只是提高长序列预测精度。

代表：MF-CLR（ICML 2024），处理多频率时序的自监督表示；TimeDART（ICML 2025），兼顾全局演化与局部模式。TimeDART 已在现有时序批次中，无需重复下载。

电池适配重点：掩码应模拟实际可观测的 SOC 窗口与传感器缺失；随机缩放、裁剪可能破坏容量等物理语义。预训练是否使用目标域无标签数据需要明确报告，不能混同严格未见域泛化。

## 2. 不规则序列、局部曲线与函数数据：优先级高

把 Q(V)、V(t)、静置电压等视为在物理坐标上采样的函数，而不是默认完整且等间隔的数组。支持观测坐标、时间间隔、掩码及局部区间输入，减少对统一插值的依赖。

代表：Hi-Patch（ICML 2025）、BayOTIDE（ICML 2024）、SAND（NeurIPS 2024）。Hi-Patch 处理不规则多变量多尺度结构；BayOTIDE 结合函数分解与贝叶斯在线插补；SAND 为稀疏含噪函数数据引入平滑归纳偏置。

适合随机起始 SOC、短时检测和跨设备采样率不同的电池场景。必须区分内部缺点、噪声与整段 SOC 区间不可见，三者难度不同。插补后的平滑曲线并不等于真实观测，需通过下游寿命任务及不同缺失机制验证。

## 3. 小样本表格学习与特征—曲线融合：优先级高

当输入已提取为容量差、内阻、静置斜率、温度统计和协议描述时，任务实质上是小样本表格回归。适合补充参数高效集成、检索增强和可解释特征选择；以 CatBoost、梯度提升树、Elastic Net 和简单 MLP 为基线。

代表：TabM（ICLR 2025），以参数高效集成降低过拟合；TabICL（ICML 2025）可作为表格上下文学习的架构参考，但该原论文针对分类，不应直接当作已经支持电池寿命回归的模型。

可研究曲线嵌入与工况/材料特征的融合。所有特征标准化、选择、检索库与调参均应限定在相应训练划分内。不能仅因使用了表格基础模型就认定小样本回归更强。

## 4. 不确定性量化与共形预测：优先级高

从单个寿命数值扩展为 RUL 区间、整条容量轨迹的预测带，或指定时间内跌破容量阈值的概率。尤其适合二次利用分级、风险筛选，以及 iMOE 当前缺少预测区间的情况。

代表：Conformalized Adaptive Forecasting of Heterogeneous Trajectories（ICML 2024）和 Multi-Source Conformal Inference Under Distribution Shift（ICML 2024）。前者关注整条轨迹的同时覆盖，后者研究多来源分布偏移下的区间。

应同时报告覆盖率、区间宽度与按工况分组的覆盖。单个循环的点态区间不等于整条退化曲线的同时预测带；普通共形预测的交换性条件也不会自动在未见材料或工况下成立。需以电芯为单位处理划分/校准，并明确跨域方法的额外假设。

## 5. 生存分析、删失数据与动态寿命预测：条件满足时优先级高

如果某电芯在尚未达到 EOL 阈值时结束测试，其寿命是“至少大于已观测时长”，不宜把最后记录循环当作真实寿命或直接丢弃。生存分析可以使用此类右删失信息，并在后续新观测到来时更新寿命分布。

代表：Dynamic Survival Analysis with Controlled Latent States（ICML 2024），结合静态变量和不规则时序；Doubly Robust Conformalized Survival Analysis with Right-Censored Data（ICML 2025），从删失数据构造寿命预测下界，不能将其结果直接写成双侧区间保证。

先核验本地各数据集是否存在删失及停止原因。异常终止与健康状态相关时，非信息性删失假设可能失效。若所有电芯都有完整、可靠的 EOL，生存分析对本任务的增益需与常规回归比较，优先级可下调。

## 暂列第二梯队

- 元学习/少样本适应：适合新材料、新协议仅有少量已标注电芯的设置，但先检查任务数量与任务间差异是否足够；不能把同一电芯的多个窗口当作多个独立任务。
- 因果推断与反事实退化预测：适合研究改变充电倍率/温度后的寿命变化，需要工况可比性与混杂控制。工况作为输入的预测模型、专家权重和注意力可视化本身不构成因果识别。
- 主动学习/实验设计：适合决定下一只电芯或下一种协议该测什么；收益取决于是否真的能追加测试。
- 扩散生成/数据增强：可辅助建模和预训练，但合成循环不会增加独立电芯数量，不能将生成数据当作独立测试证据。

## 下一轮文献补充建议

优先建立四个方法类：`RepresentationLearning`、`IrregularFunctional`、`TabularLearning`、`Uncertainty`；每类先补 15–20 篇有明确新方法的论文，避免与已有时序/GNN/PINN 批次重复。`Survival` 先补 10–15 篇，并核查本地删失情况。类别名仅为候选，正式下载时应检查命名登记表并接续永久编号。

以“数据问题—方法—任务—评估假设”组织 wiki。例如：局部 SOC → 函数编码/掩码表征 → SOH/RUL → 未见电芯、不同观测窗口；未知未来工况 → 条件轨迹预测/情景分析 → 退化轨迹 → 已知工况与未知工况分别评估。多方法论文应通过标签关联，避免同一 PDF 按方法类别重复保存。

## 已核实的论文来源

1. [Dynamic Survival Analysis with Controlled Latent States](https://proceedings.mlr.press/v235/bleistein24a.html), ICML 2024。方向：动态寿命与生存分析。
2. [MF-CLR: Multi-Frequency Contrastive Learning Representation for Time Series](https://proceedings.mlr.press/v235/duan24b.html), ICML 2024。方向：自监督表征。
3. [BayOTIDE: Bayesian Online Multivariate Time Series Imputation with Functional Decomposition](https://proceedings.mlr.press/v235/fang24d.html), ICML 2024。方向：缺失数据与函数建模。
4. [Multi-Source Conformal Inference Under Distribution Shift](https://proceedings.mlr.press/v235/liu24ag.html), ICML 2024。方向：跨域不确定性。
5. [Conformalized Adaptive Forecasting of Heterogeneous Trajectories](https://proceedings.mlr.press/v235/zhou24l.html), ICML 2024。方向：轨迹不确定性。
6. [Hi-Patch: Hierarchical Patch GNN for Irregular Multivariate Time Series](https://proceedings.mlr.press/v267/luo25r.html), ICML 2025。方向：不规则多尺度序列。
7. [TabICL: A Tabular Foundation Model for In-Context Learning on Large Data](https://proceedings.mlr.press/v267/qu25d.html), ICML 2025。方向：表格基础模型。
8. [Doubly Robust Conformalized Survival Analysis with Right-Censored Data](https://proceedings.mlr.press/v267/sesia25a.html), ICML 2025。方向：删失寿命与校准。
9. [TimeDART: A Diffusion Autoregressive Transformer for Self-Supervised Time Series Representation](https://proceedings.mlr.press/v267/wang25r.html), ICML 2025。方向：自监督表征。
10. [TabM: Advancing tabular deep learning with parameter-efficient ensembling](https://proceedings.iclr.cc/paper_files/paper/2025/hash/c1ba41c694834aeef91ae161711d4939-Abstract-Conference.html), ICLR 2025。方向：小样本表格学习。
11. [SAND: Smooth imputation of sparse and noisy functional data with Transformer networks](https://papers.nips.cc/paper_files/paper/2024/hash/f1e115bc9e9d700b93ce24f0d4f640ad-Abstract-Conference.html), NeurIPS 2024。方向：曲线与函数数据。

## 本地依据

- `battery-rul-paper/experiments/BatteryLife/data.py`：协议描述、循环字段和早期循环特征提取。
- `battery-rul-paper/experiments/README.md`：MATR 固定 cycle-100 RUL 任务与电芯/协议划分边界。
- `电池资源库/电芯/数据集/MATR_README.md`：电芯数、温度与协议构成。
- `research-wiki/papers/huang2026_imoe_prediction_secondlife.md`：局部曲线、静态特征、未来工况输入及不确定性/因果解释的限制。

本次未对原始电芯数据作全量缺失率、删失率或曲线长度审计；上述问题是否存在及其比例仍需数据核验。
