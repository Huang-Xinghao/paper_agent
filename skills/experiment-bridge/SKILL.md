---
name: experiment-bridge
description: 将电池实验计划实现为代码，调用本地执行子 skill 完成试运行、正式实验、结果核对与论文交接。
---

# 电池实验桥接

## 任务边界

参考 ARIS 的 implement → sanity → deploy → collect → handoff 流程，负责从实验计划到真实结果的完整周期。
本节点实现代码，并调用本地 `run-experiment` 子 skill 的检查与运行工具；完成结果收集和计划核对后，直接交给 `paper_plan`。
`check_experiments` / `run_experiments` 是内部工具，不是需要用户单独启动的后续节点。不接入外部评审。

```text
实验计划 → 实现 → smoke → 正式运行 → 收集结果 / 核对原计划 → 论文规划
                    ↑                 │
                    └── 修复或补任务 ──┘
```

## Inputs

本节点读取以下输入：

1. **`refine-logs/EXPERIMENT_PLAN.md`**：实验类别、数据划分、实验设置及计划产物，是实现实验的主要依据。
2. **`refine-logs/EXPERIMENT_TRACKER.md`**：具体运行任务、实验 ID、状态及依赖，用于映射 manifest jobs。
3. **`refine-logs/FINAL_PROPOSAL.md`**：研究任务、方法定义和关键机制，用于确认代码实际实现的内容。
4. **`experiments/BatteryLife/`**：项目内已准备好的参考代码和配置，直接在此基础上修改和扩展。
5. **系统提示中的运行上下文**：`BatteryLife experiment template context` 提供模板绝对路径
   `template_path`；runner 上下文提供 Python 路径、指定设备、线程和预算约束。
6. **资源库的数据登记信息**：真实数据路径、字段及数据集说明。
7. **workflow 提供的修复反馈（如有）**：失败任务的日志路径与修复次数。
   重新进入本节点时先读取反馈及对应代码，逐项修复，在 `experiments/README.md` 记录
   处理说明和修改文件。保留方案、已有测量结果、执行记录及用户资源配置；执行失败
   的修复先定位日志和调用链，不改动已有结果。可以按原计划追加遗漏的分析任务；改变实验设计时明确报告重新规划的原因。


## Workflow

### Phase 1: Parse the Experiment Plan

先读取 `EXPERIMENT_PLAN.md`，再对照 `EXPERIMENT_TRACKER.md` 提取：

1. **实验范围与执行顺序**：实验及编号、具体运行任务、前置依赖和已有完成状态。
2. **每个实验的设置**：
   - 数据来源、实体范围、train/validation/test 或校准 split 的用途。
   - 模型、基线、消融或不确定性变体，以及需要实现的数据分析内容。
   - 输入特征、预测目标、窗口、超参数、种子及评价指标。
   - 计划产物与每次运行的输出目录。
3. **运行约束**：从系统提示读取指定设备、runner Python、线程与有效预算，核对计划是否可执行。
4. **方法定义**：读取 `FINAL_PROPOSAL.md`，明确标签定义、模型结构、损失函数及关键机制。
5. **完成标准与优先级**：区分必须执行与可选项，提取各实验的成功/失败判据，以及
   计划图表需要的逐样本、逐电芯、逐域、逐种子和统计数据。

区分实验类别的呈现顺序与任务的实际依赖顺序。计划与 tracker 有冲突或方法定义
不足以实现时，记录具体问题，不自行增加实验或用占位实现补齐。

在 `experiments/README.md` 记录实现入口，同时维护 `experiments/coverage.json` 的计划覆盖记录（见下方契约）。
按实际证据任务划分：同一实验块内的训练、逐电芯/逐域统计、bootstrap、复杂度测量可能有不同入口和完成状态，不能只用“B1 已实现”覆盖全部。
相同流程可合并，不必逐个超参数展开；不同 split、域、种子和变体仍须可追溯。
tracker 用于补充细节，不能因 tracker 漏列就省略原计划必做项。

### Phase 2: Read the Existing BatteryLife Code

编码前，按计划涉及的模块阅读项目内 `experiments/BatteryLife/`：

1. **了解目录与入口**：读取 `README.md`、`assets/Data_structure_description.md`、
   `assets/Model_training.md` 和相关启动脚本，找到训练与评估的实际入口及配置来源。
2. **追踪数据路径**：从入口读取实际调用的 `data_provider/data_split_recorder.py`、
   Dataset/DataLoader 和预处理代码，确认数据字段、实体划分、标签构造、输入形状
   与归一化逻辑。结合登记的数据说明，必要时采样核验真实字段。
3. **追踪模型与训练**：读取计划涉及的 `models/`、`layers/`、`run_main.py` 和相关
   `utils/`，确认前向输出、损失函数、训练循环、验证选型及 checkpoint 保存逻辑。
4. **追踪评估与产物**：读取 `evaluate_model.py` 及相关实现，确认 checkpoint 和
   scaler 重载、真实标签来源、指标计算、测试路径及输出位置。
5. **建立计划到代码的映射**：逐项记录数据、split、标签、模型、训练、指标和变体
   对应的文件与入口，以及哪些直接复用、哪些修改或新增，再开始编码。


### Phase 3: Implement Experiments on BatteryLife

对于每个里程碑（按顺序），编写实验脚本：

1. **检查现有代码** — 查找现有实验脚本、模型代码、数据加载器。尽可能重复利用。

2. **实施缺失的部分：**
   - 具有适当 argparse 的训练脚本（所有超参数均可配置）
   - 计算指定指标的评估脚本
   - 如果需要，数据加载/预处理
   - 基线实现（如果尚未存在）
   - 修复随机种子以提高可重复性
   - 结果保存到 JSON/CSV 以供以后分析
   - 正确的日志记录（wandb 如果在 CLAUDE.md 中配置）

3. **遵循计划的运行顺序**

4. **部署前的自审稿：**
   - EXPERIMENT_PLAN.md 中的所有超参数是否都反映在 argparse 中？
   - 随机种子是固定可控的吗？
   - 结果是否以可解析的格式保存（JSON/CSV）？
   - 代码与 FINAL_PROPOSAL.md 的方法描述匹配吗？
   - split 是否真的隔离了计划要求的电芯/域，归一化和模型选择是否只使用合法训练/验证数据？
   - 标签是否来自数据集真实目标，而不是另一模型预测；所有基线/消融是否使用相同输入和评价口径？
   - 每个 seed 是否独立训练并保存结果；统计是否使用计划规定的电芯/样本等单位？

### Phase 3.5: Sanity Check

通过 `skill(name="run-experiment")` 读取本地执行子 skill，再调用 `check_experiments()`。
先验证最小真实数据上的数据路径、前向/训练、指标和产物格式。成功回执才代表 smoke 通过。
失败时读取工具返回的日志，定位并修复，再检查；遵守系统提供的修复次数和资源预算。
不能为通过 smoke 而移除正式实验机制或改用虚构测量。

### Phase 4: Deploy Full Experiments

调用 `run_experiments()`，由现有本地队列执行 manifest；它统一处理小批量和大批量、任务依赖、多 GPU 分配、OOM 重试及断点恢复。
这里不照搬 ARIS 的 SSH、screen、云 GPU 或不存在的斜杠命令，也不通过 bash 绕开队列启动训练。

按里程碑组织任务：sanity → 数据准备 → 基线/主方法 → 消融 → 统计与诊断，实际顺序由计划依赖决定。
独立的模型/种子可以并行；一张卡一个训练，设备、并行数、线程和预算使用用户现有配置，不固定卡数。
统计后处理同样是 job，通过 `depends_on` 等待上游预测；任务的 `expected_outputs` 登记实际统计产物，不能只登记“已运行”回执。
计划要求的 bootstrap、置信区间、逐域误差、失败案例和资源测量必须真正参与计算；把次数或门槛写入 JSON 不等于已经计算。

数据准备后才能确定的任务可先列入 `deferred_jobs`。第一轮运行返回后读取真实数据，再展开任务、更新覆盖记录，重新 check 和 run。
因实际门槛不满足而按计划跳过的任务，保存真实门槛计算结果与理由，再解除对应 deferred 项；不能把门槛固定写为 false。
每次修改代码或 manifest 后重新 smoke。队列复用已有完成任务，只补跑新增、失败或依赖失效的任务。若修改已经成功任务的计算逻辑，为受影响任务使用新的 job ID 和输出目录，避免同命令误复用旧结果；仅追加统计不改变原训练任务。

### 第五阶段：收集初步结果

实验完成后：

1. **解析输出文件** ( JSON/CSV/logs ) 以获得关键指标
2. **训练质量检查** — 读取训练日志和保存的指标，检查 NaN、发散、停滞或过拟合；已有 W&B 配置时可结合其记录，不要求额外服务。
3. **更新 `refine-logs/EXPERIMENT_TRACKER.md`** — 填写状态和注释列
4. **检查 EXPERIMENT_PLAN.md 的成功标准** — 每个实验是否都达到了标准？
5. **对照原计划查漏补缺**：逐项检查必做证据及图表所需数据。已有预测足够时补后处理，不因缺汇总 CSV 就删实验或重新训练。遗漏则补代码、job 与依赖，再 check → run → collect。
6. **写出 `refine-logs/EXPERIMENT_RESULTS.md`：**

```markdown
# 初步实验结果

**日期**：[今天]
**计划**：refine-logs/EXPERIMENT_PLAN.md

## 按里程碑划分的结果

### M0：基本正确性 — 通过
- [结果]

### M1：基线
| 运行 | 系统 | 关键指标 | 地位 |
|-----|--------|-----------|--------|
| R001 | 基线_1 | XX | 完毕 |

### M2：主要方法
| 运行 | 系统 | 关键指标 | 地位 |
|-----|--------|-----------|--------|
| R003 | 我们的方法 | XX | 完毕 |

### M3: 消融实验
...

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


交接前重新读取完整实验计划，核对必做项 X/Y、条件跳过及可选项；同步 tracker、coverage 和结果摘要。
每个原计划图/子图的证据都要能指向实际产物，或明确指出尚缺什么。必做缺口不能留给 paper_plan 静默删减。
必做项未完成则继续补齐；确实缺数据/资源/预算时报告阻塞，不宣称 bridge 完成。

## 计划覆盖契约

`experiments/coverage.json` 是简短列表，与 README/Tracker 共用任务 ID；不需要新增复杂报告系统。例如：

```json
[
  {
    "id": "B1-paired-statistics",
    "plan_ref": "B1 指标与图3(b)：逐电芯配对差值和置信区间",
    "required": true,
    "status": "planned",
    "job_ids": ["paired_statistics"],
    "outputs": ["results/statistics/paired_cells.csv", "results/statistics/bootstrap.json"]
  }
]
```

- `status` 为 `planned`、`completed`、`blocked` 或 `skipped`。实现后待运行为 planned；有真实执行结果后才标 completed。
- planned/completed 必须映射到 manifest 的 job ID 和它检查的 JSON/CSV 产物；不要只指向包含次数、配置或 pass 标志的回执。
- blocked/skipped 必须写 `reason`。必做项只有满足原计划条件才能 skipped，并在 `evidence` 中列出真实门槛测量 JSON/CSV 路径。
- 未展开的必做任务先登记 blocked，并在 manifest 的 deferred_jobs 中说明解锁条件；后续由本节点自动处理。
- Runner 会核验登记任务的完成回执、产物与必做状态；原计划是否全部映射、统计含义和跳过理由仍须由你阅读原文逐项核对。


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
  "smoke": {
    "id": "smoke",
    "argv": ["python", "experiments/run.py", "--smoke"],
    "resources": {"device": "cpu"},
    "timeout_seconds": 600,
    "expected_outputs": ["smoke-results/smoke.json"]
  },
  "jobs": [
    {
      "id": "exp_2_1_HUST_full_seed_17",
      "argv": ["python", "experiments/run.py", "--experiment-id", "2.1", "--seed", "17", "--output-dir", "results/2_overall_performance/2.1_HUST_full/seed_17"],
      "resources": {"device": "auto"},
      "timeout_seconds": 3600,
      "expected_outputs": ["results/2_overall_performance/2.1_HUST_full/seed_17/targets.csv", "results/2_overall_performance/2.1_HUST_full/seed_17/predictions.csv"]
    }
  ]
}
```

Manifest 使用现有本地 runner 接口：jobs 必须有独立输出路径，`depends_on` 引用同清单任务。
`expected_outputs` 为 `results/` 下含真实有限数值的 JSON/CSV，smoke 则放 `smoke-results/`。
复杂度、图表数据、统计检验等由计划决定，以上示例不是产出上限。
