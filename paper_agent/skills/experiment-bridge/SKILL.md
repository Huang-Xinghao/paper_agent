---
name: experiment-bridge
description: 将电池实验计划实现为代码，调用本地执行子 skill 完成正式实验、结果核对与论文交接。
---

# 电池实验桥接

## 任务边界

本节点实现代码，并调用本地 `run-experiment` 子 skill 的检查与运行工具；完成后，直接交给 `paper_plan`。
`run_experiments` 是内部工具，不是需要用户单独启动的后续节点。
## Inputs

本节点读取以下输入：

1. **`refine-logs/EXPERIMENT_PLAN.md`**：实验类别、数据划分、实验设置及计划产物，是实现实验的主要依据。
2. **`refine-logs/EXPERIMENT_TRACKER.md`**：runner 自动生成的块级进度视图，一块一行，仅供查看；具体 jobs 和依赖以 manifest 为准。
3. **`refine-logs/FINAL_PROPOSAL.md`**：研究任务、方法定义和关键机制，用于确认代码实际实现的内容。
4. **项目内基础模板**：读取项目已准备的参考代码和配置，在适合当前任务的模板上修改和扩展，例如 `experiments/BatteryLife/` 或 `experiments/bamboo_mixer/`。模板由当前项目的研究对象、计划和实际目录决定。
6. **资源库的数据登记信息**：真实数据路径、字段及数据集说明。
   同时读取 `data-search/DATA_REPORT.md`（若有），利用数据包及配套文件索引定位标签、元数据、划分和加载代码；未在电芯子目录找到标签不能据此判断数据包缺失标签。



## Workflow

### Phase 1: Parse the Experiment Plan

先读取 `EXPERIMENT_PLAN.md`，再对照 `EXPERIMENT_TRACKER.md` 提取：

1. **实验范围与执行顺序**：D/B 块及稳定编号、需要展开的具体 jobs、实际前置依赖和已有执行回执。
2. **每个实验的设置**：
   - 数据来源、实体范围、train/validation/test 。
   - 模型、基线、消融或不确定性变体，以及需要实现的数据分析内容。
   - 输入特征、预测目标、窗口、超参数、种子及评价指标。
   - 计划产物与每次运行的输出目录。
3. **方法定义**：读取 `FINAL_PROPOSAL.md`，明确标签定义、模型结构、损失函数及关键机制。
4. **完成标准与优先级**：区分必须执行与可选项，提取各实验的成功/失败判据。

区分实验类别的呈现顺序与任务的实际依赖顺序。计划是实验设计依据，tracker 是执行状态视图。方法定义
不足以实现时，记录具体问题，不自行增加实验或用占位实现补齐。

### Phase 2: Read the Project Base Template

编码前，确认项目实际采用的基础模板：

1. **了解目录与入口**：查看已有说明（若有）、配置和启动脚本，找到数据处理、训练、评估或生成的实际入口。
2. **追踪数据路径**：阅读实际调用的数据加载、划分和预处理代码，确认字段、样本单位、标签及掩码、输入形状、单位与归一化逻辑；
3. **追踪模型与训练**：阅读计划涉及的模型、损失函数和训练入口，确认前向输出、可训练部分、验证选型。
4. **追踪评估与产物**：确认权重和预处理参数重载、真实标签来源、指标计算、测试路径及输出位置；涉及生成任务时核对条件和输出定义。
5. **建立计划到代码的映射**：逐项记录数据、split、标签、模型、训练、指标和变体对应的实际文件与入口，以及哪些复用、修改或新增，再开始编码。


### Phase 3: Implement Experiments on the Base Template

对于每个里程碑，编写实验脚本：

1. **检查现有代码** — 查找现有实验脚本、模型代码、数据加载器。尽可能重复利用。参考`电池资源库/代码和绘图指南/代码设计.md`来设置batchsize和线程数

2. **实施缺失的部分：**
   - 具有适当 argparse 的训练脚本（所有超参数均可配置）
   - 计算指定指标的评估脚本
   - 如果需要，数据加载/预处理
   - 基线实现（如果尚未存在）
   - 修复随机种子以提高可重复性
   - 结果保存到 JSON/CSV 以供以后分析

3. **遵循计划的运行顺序**

4. **部署前的自审稿：**
   - EXPERIMENT_PLAN.md 中的所有超参数是否都反映在 argparse 中？
   - 随机种子是固定可控的吗？
   - 结果是否以可解析的格式保存（JSON/CSV）？
   - 代码与 FINAL_PROPOSAL.md 的方法描述匹配吗？
   - split 是否真的隔离了计划要求的实体/域，归一化和模型选择是否只使用合法训练/验证数据？
   - 标签是否来自数据集真实目标，而不是另一模型预测；所有基线/消融是否使用相同输入和评价口径？
   - 每个 seed 是否独立训练并保存结果；统计是否使用计划规定的样本与统计单位？

### 独立代码审阅

- 模型、数据处理、训练、评估及 manifest 的**完整调用链**准备好后（禁止写部分代码就调用），调用 `review_code`，通过 `files` 传入相关代码、配置和测试文件路径；
- reviewer 只审阅，修复由你完成。报告写入 `refine-logs/CODE_REVIEW.md`，正文返回当前对话。
- 结合证据处理审阅意见，优先修复影响实验有效性的问题；有依据时可以不采纳，关键分歧简要说明。
- 核心实现有较大修改时，可再次审阅。审阅执行成功不等于代码正确，运行正确性以实际执行回执和结果为准；调用失败时查看错误并重试，**最多review次数为两次**。
- 当因为某些原因报错后重启节点，实验已经开始运行或已完成时，**跳过这一步**

### Phase 4: Execute Experiments

通过 `skill(name="run-experiment")` 读取并遵循本地执行子 skill。
运行工具、日志定位、失败重试和续跑规则统一由该 skill 说明。
执行结束后回到本阶段解释结果并核对原计划的证据覆盖。

### 第五阶段：收集初步结果

实验完成后：

1. **解析输出文件** ( JSON/CSV/logs ) 以获得关键指标
2. **训练质量检查** — 读取训练日志和保存的指标，检查 NaN、发散、停滞或过拟合；已有 W&B 配置时可结合其记录，不要求额外服务。
3. **读取 `refine-logs/EXPERIMENT_TRACKER.md`** — runner 按 D/B 块自动汇总进度，不要手动编辑；未部署块仍会保留。
4. **检查 EXPERIMENT_PLAN.md 的成功标准** — 每个实验是否都达到了标准？
5. **对照原计划查漏补缺**：逐项检查必做证据及图表所需数据。已有预测足够时补后处理，不因缺汇总 CSV 就删实验或重新训练。遗漏则补代码、job 与依赖，再按执行子 skill 运行并收集结果。
6. **写出 `refine-logs/EXPERIMENT_RESULTS.md`：**

```markdown
# 初步实验结果

**日期**：[今天]
**计划**：refine-logs/EXPERIMENT_PLAN.md

## 按 D/B 块划分的结果

### D1：数据集展示
- 证据：[实际分析数据与图表输入路径]
- 观察与局限：……

### B1：总体性能
- 证据：[统计表、各配置原始结果、预测与真实值路径]
- 主要数值：[按相同数据集、split、模型/变体分组汇总多个 seed]
- 是否满足原计划成功标准：……
- 结论与局限：……

### B2：消融实验
（按原计划块继续，保留全部必做块，明确尚缺证据。）

## 概括
- [ X/Y ] 必须执行实验已完成；按条件跳过：[ID、门槛结果与证据路径]
- 主要结果：[ positive/negative/inconclusive ]
- 准备好 paper_plan：[ YES/NO ]

## 下一步
→ paper_plan
```

负结果不等于实验未完成。分别记录执行状态、方法成功判据和条件跳过，不为得到正结果回看 test 调参。
保留原计划消融；不能因主模型结果为负就跳过原定必做项。额外实验超出原计划时记录建议，不自行扩大范围。

### 阶段 6：交接

目前最终状态：

```
🔬 实验桥完成：
- 已实施：[N] 个实验脚本
- 已部署：在 [M] 个 GPU 上进行 [N] 次实验
- 已完成: [ X/Y ] 必须执行, [ A/B ] 很好
- 主要结果：[一句话]

结果：refine-logs/EXPERIMENT_RESULTS.md
追踪器：refine-logs/EXPERIMENT_TRACKER.md
```


交接前重新读取完整实验计划，核对必做项 X/Y、条件跳过及可选项；核对 runner 生成的 tracker，并更新结果摘要；不要手动改写 tracker。
每个原计划图/子图的证据都要能指向实际产物，或明确指出尚缺什么。必做缺口不能留给 paper_plan 静默删减。
必做项未完成则继续补齐；


## 最小产物

以下是基础产物，不是实验计划的产出上限。数据集分析保存计划所需的分析数据。
总体性能、对比、消融和不确定性实验至少保存：

- 模型 checkpoint；复用模型时引用已有文件，无训练参数的方法注明不适用。
- 真实值 CSV 和预测值 CSV，通过一致的样本 ID 关联到评测样本。
- 模型超参数配置，能够关联到对应 checkpoint 和预测结果。

产物按计划统一写入
`results/<实验类别>/<实验ID_数据集_模型或变体>/seed_<种子>/`；实验类别使用
`1_data_analysis`、`2_overall_performance`、`3_comparison`、`4_ablation`、
`5_uncertainty`。同一实验配置的不同种子放在同一父目录下，数据集、split、模型或
变体不同的配置分开建目录。每个种子目录集中保存该次运行的 checkpoint、配置、
真实值、预测值及所需 scaler

## Manifest 契约

写入 `experiments/manifest.json`，例如：

```json
{
  "backend": "local",
  "required_inputs": ["项目内相对输入路径或已登记外部数据绝对路径"],
  "jobs": [
    {
      "id": "B1_HUST_full_seed17",
      "block_id": "B1",
      "depends_on": [],
      "argv": ["python", "experiments/run.py", "--experiment-id", "B1", "--seed", "17", "--output-dir", "results/2_overall_performance/B1_HUST_full/seed_17"],
      "resources": {"device": "auto"},
      "timeout_seconds": 3600,
      "expected_outputs": [
        {"path": "results/2_overall_performance/B1_HUST_full/seed_17/targets.csv"},
        {"path": "results/2_overall_performance/B1_HUST_full/seed_17/predictions.csv"},
        {"path": "results/2_overall_performance/B1_HUST_full/seed_17/metrics.json", "metrics": ["mae"]}
      ]
    }
  ]
}
```

正式 job 必须通过 `block_id` 关联计划中的 D/B 块。
不要再使用 R001 等另一套实验编号。job ID 标识具体执行任务，块 ID 标识研究设计。
独立任务显式填写 `depends_on: []`；省略依赖会保留按 manifest 顺序串行的既有行为。
跨块复用 checkpoint 等产物时，消费者通过 `depends_on` 依赖生产 job，不要重复训练。
统计汇总作为后处理 job：只对可比较配置的多个 seed 聚合，不把同块不同模型混为一组。
`expected_outputs` 的每项必须是含 `path` 和可选 `metrics` 的对象，不能写成路径字符串。
