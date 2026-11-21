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
4. **项目内基础模板（可选）**：读取项目已准备的参考代码和配置，在适合当前任务的模板上修改和扩展，默认使用 `experiments/template/` 的 IC2ML 通用框架。模板由当前项目的研究对象、计划和实际目录决定。
5. **资源库的数据登记信息**：用 `battery_resources(action="search", query="数据集名称")` 获取说明与登记路径，`action="read"` 配合 resource_id 阅读说明；实现加载器需要核对样本时，用 `action="inspect"` 配合 path 查看结构。
   同时读取 `data-search/DATA_REPORT.md`，利用数据包及配套文件索引定位标签、元数据、划分和加载代码；未在电芯子目录找到标签不能据此判断数据包缺失标签。

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
4. **完成标准**：提取各实验的成功/失败判据。

区分实验类别的呈现顺序与任务的实际依赖顺序。计划是实验设计依据，tracker 是执行状态视图。方法定义
不足以实现时，记录具体问题，不自行增加实验或用占位实现补齐。

### Phase 2: Prepare and Understand the Base Template

#### 2.1 选择或复用代码

从实验计划的“模板与实现交接”提取研究对象、任务、数据路径、划分及待实现模块。
已有适用代码时沿用实际目录；新项目调用 `prepare_experiment_template` 准备可编辑代码：

```text
domain="cell", tasks=["life"], dataset="Tongji",
data_path="Data Report 已核实的数据根路径", destination="experiments/template"
```

电芯任务为 `life/soh/trajectory/rul`，电解液任务为 `conductivity/anion_ratio`，按计划选择单任务或组合。
电解液使用 `domain="electrolyte", dataset="bamboo_mixer"`，现成模型是 CompositionMLP。
工具返回配置、入口、工作目录与启动命令；依赖和数据位置结合当前执行环境确认。续跑复用已复制并修改的代码。

#### 2.2 阅读实际调用链

| 位置 | 重点确认 |
| --- | --- |
| `task_config.json`、`configuration.py` | 实际任务、数据路径、窗口、训练参数和命令行覆盖方式 |
| `adapters/<domain>.py`、`data_provider/` | 输入输出、标签单位、mask、划分、归一化和样本 ID |
| `models/registry.py`、模型文件 | 模型构造、前向接口、可训练部分和任务输出 |
| `exp/experiment.py`、`run.py` | 训练、验证、checkpoint 恢复、评价口径和结果保存 |

电芯默认沿用 BatteryLife 固定划分；电解液默认按配方分组，支持 `split_file`。
将计划中的样本单位、观测窗口、标签及评价单位与加载器逐项对应。

#### 2.3 确认扩展位置

- **新增模型**：在 `models/` 实现并加入注册表，同步 adapter 的 `compatible_models`，使输入和任务输出对齐。
- **特殊划分**：按计划在加载器中接入明确的实体名单或折号，并记录实际划分来源。
- **统计口径**：模板默认按窗口/观测值计算指标；电芯级、固定早期窗口或其他聚合指标按计划增加筛选和后处理。

将复用、修改和新增部分简要对应到计划 D/B 块，作为实现与后续审阅的依据。

### Phase 3: Implement Experiments on the Base Template

#### 3.1 实现计划中的方法

围绕已读取的调用链补齐模型、基线、消融、损失及数据处理。任务设置写入配置，训练参数可通过命令行覆盖。
batch size 和线程数结合实际算力，参考资源库中的 `代码和绘图指南/代码设计.md`。

#### 3.2 分开训练验证与最终测试

模板支持以下运行方式：

| `--stage` | 用途与产物 |
| --- | --- |
| `train` | 训练并恢复最佳验证 checkpoint，保存 `validation_metrics.json` 和验证预测 |
| `validate` | 恢复 checkpoint，计算验证指标 |
| `test` | 恢复 checkpoint，保存最终测试指标与预测 |
| `train_test` | 已确定配置时，依次训练、验证和测试 |

候选搜索使用 `train` 返回的验证指标，保存最佳参数文件；正式配置读取该文件训练，再由依赖任务执行 `test`。
寻优按计划实现为普通 manifest 任务，用 `depends_on` 关联候选评估、选参和正式训练，保存真实配置路径、候选指标和实际生效参数。嵌套验证按外折和模型分别保存选择结果，测试数据不参与选参；后端不解析 hpo-policy，也不自动寻优或绑定最佳配置。
训练早停默认使用所选任务的加权验证损失，候选选参指标按计划读取，例如 `life.mae`。

manifest 的命令从项目根目录执行。使用工具返回的 `project_train_argv` 和 `project_test_argv`，按 job 补上配置、种子和 `--output_dir`。
例如：

```text
python experiments/template/run.py --config experiments/template/task_config.json \
  --stage train --tasks life --seed 17 \
  --output_dir results/2_overall_performance/B1_Tongji_IC2ML/seed_17
```

最终测试使用相同的模型和数据配置，通过 `--checkpoint` 指向该训练目录。

#### 3.3 组织结果与验证实现

每组训练配置和种子使用独立结果目录，配套训练与测试共用该目录。
`data_manifest.json` 记录实际参数、预处理和划分，CSV 通过 `sample_id + task + target_index` 关联真实值与预测。

正式执行前，围绕以下项目核对实现：

- **方法**：模型、损失和关键消融对应 FINAL_PROPOSAL 与实验计划。
- **数据**：划分、预处理、观测范围和标签定义对应 Data Report；各方法使用一致的评价样本。
- **运行**：命令可解析配置与种子，资源设置对应项目配置。
- **产物**：指标字段、CSV、checkpoint 和样本标识与 manifest 对应，统计使用计划规定的单位。

### 独立代码审阅

- 模型、数据处理、训练、评估及 manifest 的**完整调用链**准备好后，调用 `review_code`，通过 `files` 传入相关代码、配置和测试文件路径；
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

1. **解析输出文件** 以获得关键指标
2. **训练质量检查** — 读取训练日志和保存的指标，检查 NaN、发散、停滞或过拟合；已有 W&B 配置时可结合其记录，不要求额外服务。
3. **读取 `refine-logs/EXPERIMENT_TRACKER.md`** — runner 按 D/B 块自动汇总进度，不要手动编辑；未部署块仍会保留。
4. **检查 EXPERIMENT_PLAN.md 的成功标准** — 每个实验是否都达到了标准？
5. **写出 `refine-logs/EXPERIMENT_RESULTS.md`：**

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
- [ X/Y ] 计划实验已完成；按条件跳过：[ID、条件判断结果与证据路径]
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
- 已完成：[ X/Y ] 计划实验
- 主要结果：[一句话]

结果：refine-logs/EXPERIMENT_RESULTS.md
追踪器：refine-logs/EXPERIMENT_TRACKER.md
```

每个原计划图/子图的证据都要能指向实际产物，或明确指出尚缺什么。必做缺口不能留给 paper_plan 静默删减。
可修复的必做缺口继续补齐；无法完成时如实写出缺口及其对结论的影响，交由主控决定后续方向，不增设回执审批。


## 最小产物

以下是基础产物，不是实验计划的产出上限。数据集分析保存计划所需的分析数据。
总体性能、对比、消融和不确定性实验至少保存：

- 模型 checkpoint；复用模型时引用已有文件，无训练参数的方法注明不适用。
- `targets.csv` 和 `predictions.csv`，通过 `sample_id + task + target_index` 关联到评测样本；`observed` 标记真实标签是否可用。
- `data_manifest.json` 中的实际参数、预处理状态和划分，以及对应指标；已有 NPY 与样本 JSON 可保留供后处理使用。

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
  "required_inputs": ["experiments/template/task_config.json", "已登记的数据根路径"],
  "jobs": [
    {
      "id": "B1_Tongji_IC2ML_train_seed17",
      "block_id": "B1",
      "depends_on": [],
      "argv": ["python", "experiments/template/run.py", "--config", "experiments/template/task_config.json", "--tasks", "life", "--stage", "train", "--seed", "17", "--output_dir", "results/2_overall_performance/B1_Tongji_IC2ML/seed_17"],
      "resources": {"device": "auto"},
      "timeout_seconds": 3600,
      "expected_outputs": [
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/checkpoint.pth"},
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/data_manifest.json"},
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/validation_metrics.json", "metrics": ["life.mae"]}
      ]
    },
    {
      "id": "B1_Tongji_IC2ML_test_seed17",
      "block_id": "B1",
      "depends_on": ["B1_Tongji_IC2ML_train_seed17"],
      "argv": ["python", "experiments/template/run.py", "--stage", "test", "--checkpoint", "results/2_overall_performance/B1_Tongji_IC2ML/seed_17"],
      "resources": {"device": "auto"},
      "timeout_seconds": 3600,
      "expected_outputs": [
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/targets.csv", "nonempty": true},
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/predictions.csv", "nonempty": true},
        {"path": "results/2_overall_performance/B1_Tongji_IC2ML/seed_17/metrics.json", "metrics": ["life.mae"]}
      ]
    }
  ]
}
```

将示例中的任务、数据路径和指标替换为当前计划的实际值。多任务指标按任务名读取，例如 `soh.rmse`、`rul.mae`。
CSV 中的向量目标由 `target_index` 区分，后处理结合样本 JSON 解释电芯、观测位置或配方信息。

正式 job 必须通过 `block_id` 关联计划中的 D/B 块。
不要再使用 R001 等另一套实验编号。job ID 标识具体执行任务，块 ID 标识研究设计。
独立任务显式填写 `depends_on: []`；省略依赖会保留按 manifest 顺序串行的既有行为。
跨块复用 checkpoint 等产物时，消费者通过 `depends_on` 依赖生产 job，不要重复训练。
统计汇总作为后处理 job：只对可比较配置的多个 seed 聚合，不把同块不同模型混为一组。
`expected_outputs` 的每项必须是含 `path` 和可选 `metrics` 的对象，不能写成路径字符串。
任务正常退出且产物校验通过才记为完成。JSON 对象/数组和 CSV 表允许为空；需要非空时在对应 `expected_outputs` 条目设置 `"nonempty": true`。声明的 `metrics` 字段必须存在且数值有效。
