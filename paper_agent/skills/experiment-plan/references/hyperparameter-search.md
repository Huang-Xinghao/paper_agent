# 超参数寻优

本文件是 experiment-plan 的寻优接口参考，不是独立 Skill。
experiment-plan 按需制定搜索范围、预算并绑定代表性 B 块；bridge 实现训练入口并调用运行器。

## 实验计划中的寻优策略

需要寻优时在 `refine-logs/EXPERIMENT_PLAN.md` 末尾写一个 `hpo-policy` 围栏 JSON。
正文用中文解释保持不变的核心机制、可调参数、范围依据、验证口径和预算。
学习率、隐藏维度和层数等可以作为可调参数；核心机制的有无属于正式消融。
候选数、训练轮数和时间额度按任务与算力估计，示例数值不作为默认要求。
不需要寻优时写 `{"enabled":false,"reason":"具体原因"}`，采用计划说明的固定配置。
专家模式随实验计划审阅；自动模式在项目资源限制内自行确定。

下面是接口示例，数值应按任务和算力制定：

```hpo-policy
{
  "enabled": true,
  "model_structure": "固定编码器、聚合与预测头的连接和核心机制；只调整所列维度",
  "fixed_parameters": {"optimizer": "adamw"},
  "search_space": {
    "learning_rate": {"type": "float", "low": 0.00001, "high": 0.003, "log": true},
    "hidden_dim": {"type": "categorical", "choices": [64, 128, 256]},
    "num_layers": {"type": "int", "low": 2, "high": 4}
  },
  "metric": "validation.mae",
  "direction": "minimize",
  "validation_protocol": "按电芯划分训练与验证；测试电芯不参与参数选择，归一化只拟合训练数据",
  "scope": "在主要数据集的代表性总体性能实验选优；此配置用于该数据集主模型和兼容消融",
  "candidates": 12,
  "rungs": [5, 20, 60],
  "reduction_factor": 3,
  "max_seconds": 3600,
  "trial_timeout_seconds": 600,
  "sampler_seed": 42,
  "seeds": [17, 23],
  "smoke_epochs": 1
}
```

`metric` 必须为 `validation.*`，不使用测试集选型。按研究目标隔离电芯、批次、时间或工况，
避免把同一电芯的相邻窗口随机拆到训练与验证中。`max_seconds` 是项目所有寻优尝试的累计进程秒数上限，
包括 smoke、失败和重试；还受项目总实验预算约束。为正式实验留足预算，勿把额度全分给寻优。
`trial_timeout_seconds` 不得超过项目单任务上限；时间不足时减少候选、轮数或参数维度。

## 规划代表性实验

experiment-plan 在 `EXPERIMENT_PLAN.md` 说明选择哪个 B 块、数据集和实际 train/validation 划分、
主指标、参数应用范围、基线的调参口径及成本。代表性实验应体现核心难点，不能挑最容易或测试结果最好的子集。
把可执行绑定写入 `experiments/hpo_spec.json`：

```json
{
  "block_id": "B1",
  "selection": "主要数据集总体性能；沿用正式训练/验证电芯划分，仅在验证集计算选优指标",
  "argv": ["python", "experiments/train.py", "--config", "{config}", "--output-dir", "{output_dir}"],
  "code_files": ["experiments/train.py", "experiments/model.py", "experiments/splits.json"],
  "required_inputs": ["data/train.csv", "data/validation.csv", "experiments/splits.json"],
  "resources": {"device": "auto"}
}
```

`code_files` 列出实际训练调用链、预处理、模型与划分配置，运行器用其内容指纹识别代码变化。
实际数据文件使用路径、大小和修改时间识别变化，避免每轮重读大型数据集；划分配置仍应列入 `code_files`。
`required_inputs` 沿用已登记数据路径规则。规划时可先声明将要实现的路径，bridge 完成实现后修正可执行绑定，
不扩大已确认的参数范围。训练脚本至少支持 `--config` 和 `--output-dir`；运行器替换上述占位符，
也支持 `{epochs}`、`{seed}`、`{mode}`。

## 训练接口与运行

运行器写入的 trial 配置形如：

```json
{"parameters":{"learning_rate":0.001,"hidden_dim":128,"num_layers":2,"optimizer":"adamw"},
 "epochs":20,"seed":17,"phase":"tuning","evaluation_split":"validation"}
```

训练入口必须实际使用 `parameters`、`epochs`、`seed`，只在合法训练集拟合预处理，在验证集选优。
`phase=smoke` 时用少量 batch 检查数据、前向、反向和输出链路，保存有效 `metrics.json`，分数不参与排名。
`phase=tuning` 时按指定训练额度运行，在输出目录保存 `metrics.json`，如 `{"validation":{"mae":0.25}}`。
每次运行保存实际生效配置，避免默认参数覆盖所选参数。运行器使用有限数值验证，不替代对训练代码的审阅。

代码调用链完整后沿用 bridge 现有代码审阅，然后调用 `run_hyperparameter_search()`。
第一版是带随机种子的随机采样加 successive halving：同一层训练轮数相同，按验证指标保留前一部分候选，
晋级后从头训练到下一层轮数；最后的候选在相同轮数和相同种子集合上按均值比较。
搜索树、各次参数、指标与日志路径保存在 `results/hpo/<search_id>/search.json`，最佳配置为
`results/hpo/best_config.json`。不把这种搜索描述为 MCTS、TPE 或全局最优证明。

失败时按真实日志修复实现并重试。已完成且产物有效的相同 trial 复用执行回执；失败花费也计入累计额度。
额度用尽时，仅有同层、同种子集合的完整候选可供交付，否则返回等待，不用任意默认参数冒充选优。
增加额度时更新实验计划，沿用当前模式的计划审阅或自动选定流程；已完成试跑可继续复用。`budget_limited=true` 表示搜索未完成全部计划，报告须说明。

## 固定配置并进入正式实验

在正式 `experiments/manifest.json` 顶层增加 `"hpo":{"job_ids":["B1_main_seed17","B1_main_seed23"]}`。
列出的训练 job 在 `argv` 中用独立字符串 `"{best_config}"` 接收参数文件，例如
`["python","experiments/train.py","--config","{best_config}","--seed","17","--epochs","60","--output-dir","results/2_overall_performance/B1_main/seed_17"]`。
最终配置仍通过 `parameters` 读取，正式种子和完整训练轮数由正式 job 明确指定，不沿用 smoke 模式。
每个目标 job 必须保存实际生效配置；基线、消融特有设置在计划中说明，不能直接强套主模型参数。

调用 `run_experiments()` 后，运行器先完成或复用寻优，再绑定最佳配置执行正式 jobs。
选优配置或声明的代码发生变化后，正式任务的执行指纹也变化，旧回执不会冒充新结果。
HPO 试跑不加入正式 D/B tracker，不计为正式对比、多种子结果或消融完成数。
在已有 `EXPERIMENT_RESULTS.md` 中追加寻优范围、花费、代表性条件、最佳配置路径与局限，
不另写一份交接报告；不依据正式测试结果反复扩大搜索范围。
