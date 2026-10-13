---
name: experiment-bridge
description: 将电池实验计划实现为本地实验代码、配置和 runner manifest。
---

# 电池实验桥接

## 任务边界

将实验计划实现为 `experiments/` 下的可执行实验包。本节点不启动正式实验；后续由
`experiment-check` 执行 smoke，
`experiment-run` 执行实验，完成后进入 `paper-plan`。

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
   的修复还须保留 manifest。需要改变实验设计时明确报告重新规划的原因。

修复完成后交回 workflow，由它重新执行 smoke 和正式任务；本节点不自行
调用评审或运行实验，也不将未经验证的修复标记为通过。



## Workflow

### Phase 1: Parse the Experiment Plan

先读取 `EXPERIMENT_PLAN.md`，再对照 `EXPERIMENT_TRACKER.md` 提取：

1. **实验范围与执行顺序**：五类实验及编号、具体运行任务、前置依赖和已有完成状态。
2. **每个实验的设置**：
   - 数据来源、实体范围、train/validation/test 或校准 split 的用途。
   - 模型、基线、消融或不确定性变体，以及需要实现的数据分析内容。
   - 输入特征、预测目标、窗口、超参数、种子及评价指标。
   - 计划产物与每次运行的输出目录。
3. **运行约束**：从系统提示读取指定设备、runner Python、线程与有效预算，核对计划是否可执行。
4. **方法定义**：读取 `FINAL_PROPOSAL.md`，明确标签定义、模型结构、损失函数及关键机制，


区分实验类别的呈现顺序与任务的实际依赖顺序。计划与 tracker 有冲突或方法定义
不足以实现时，记录具体问题，不自行增加实验或用占位实现补齐。

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

1. 在 `experiments/BatteryLife/` 内实现实验：数据适配和划分放 `data_provider/`
   或原有预处理位置，模型放 `models/`，共用层放 `layers/`，训练、验证、校准和
   评估扩展原有流程或在模板内新增模块。保留可复用的原始模型、划分和训练组件。
   不在模板目录外新写一套独立的数据加载、模型和训练流程；`experiments/run.py`
   如需创建，只负责参数解析和调用模板内的实际入口。
2. manifest 仍放在 `experiments/manifest.json`。runner 从项目根目录执行命令，
   而原模板部分路径依赖工作目录；通过模板内入口或薄包装适配 cwd、数据路径、
   设备、输出和 backend report。逐条追踪 smoke 与正式 job 的调用链，确认它们
   真正进入模板中按计划修改或新增的数据、模型、训练及评估代码，而非仅检查
   `BatteryLife/` 目录存在。`experiments/README.md` 记录上述计划到代码的映射、
   复用与修改的文件、实际入口及与原模板不同之处。


## 最小产物

数据集分析保存计划所需的分析数据。总体性能、对比、消融和不确定性实验只要求保存：

- 模型 checkpoint；复用模型时引用已有文件，无训练参数的方法注明不适用。
- 真实值 CSV 和预测值 CSV，通过一致的样本 ID 关联到评测样本。
- 模型超参数配置，能够关联到对应 checkpoint 和预测结果。

产物按计划统一写入
`results/<实验类别>/<实验ID_数据集_模型或变体>/seed_<种子>/`；实验类别使用
`1_data_analysis`、`2_overall_performance`、`3_comparison`、`4_ablation`、
`5_uncertainty`。同一实验配置的不同种子放在同一父目录下，数据集、split、模型或
变体不同的配置分开建目录。每个种子目录集中保存该次运行的 checkpoint、配置、
真实值、预测值及所需 scaler

文件名按实际框架选择。图表、指标汇总、统计检验和额外日志按需生成，不作为统一必需产物。

当前 runner 的 `expected_outputs` 只接受包含有效数值的 CSV/JSON 测量结果：
将预测结果或分析数据登记到该字段；checkpoint 和超参数配置单独保存并记录路径，
不作为测量结果登记。smoke 使用独立的检查结果 JSON/CSV。

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

示例中的命令、超时和文件名按实际计划调整，保留以下运行器接口约束：

- `backend` 为 `local`；`required_inputs` 为非空路径列表，列出实际依赖的已有输入。
- `smoke` 和非空 `jobs` 中的 ID 全局唯一，只使用字母、数字、下划线或连字符。
- `argv` 为非空字符串数组，命令以项目根目录为工作目录，不拼接 shell 命令。
- `resources` 仅包含 `device`，取值为 `auto`、`cpu`、`cuda` 或 `mps`；每个任务有符合预算的正数 `timeout_seconds`。
- 每个任务至少登记一个 `expected_outputs`，各任务输出路径不重复。正式结果位于 `results/`，smoke 结果位于 `smoke-results/`，扩展名为 `.csv` 或 `.json`。
- jobs 与 tracker 中的实验 ID、split、变体和产物对应，并按依赖安排执行顺序。实际阻塞或未实现的任务放入 `deferred_jobs`，不放入可执行 jobs，例如：
  `{"id": "training", "reason": "具体缺失数据或方法定义", "experiment_id": "2.1"}`。
  deferred ID 同样唯一，不包含 `argv` 或 `timeout_seconds`；存在 deferred 任务时，工作流不会进入论文生成。

如果没有任何可执行任务，明确报告阻塞原因，不用空 jobs 或伪造任务冒充完成桥接。

## 交付检查

交付 `experiments/manifest.json`、实验代码、配置和简短的 `experiments/README.md`；
README 说明运行方式、数据来源和产物位置。

确认计划中的实验已映射为可执行任务或明确的阻塞记录，manifest 符合接口，
smoke 命令及输入输出路径正确。列出项目内 BatteryLife 中实际修改/新增的
代码文件，并核对每个正式 job 会调用这些实现；仅保留模板而未实现计划、仅新增模板目录外的
`soh_pipeline.py` 或仅生成占位结果均不算交付。同步 tracker 的任务与产物信息，
交由后续节点执行。
