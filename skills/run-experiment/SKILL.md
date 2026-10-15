---
name: run-experiment
description: experiment-bridge 内部使用的本地实验执行子 skill，负责 smoke、队列运行、日志定位和断点续跑。
---

# Run Experiment

参考 ARIS 的 pre-flight → sanity → deploy → collect 流程；由 experiment-bridge 调用，不作为独立 workflow 节点。

## 1. Pre-flight

读取现有 `experiments/manifest.json`、`experiments/coverage.json` 及系统提供的设备/Python/预算上下文。
核对真实输入、命令参数、独立输出目录和依赖，代码缺失先回到 bridge 实现。遵守用户设备配置，不自行降级或更改配置。

## 2. Sanity

调用 `check_experiments()`，运行 manifest 的 smoke。检查工具返回的真实回执；失败时读取 `output.log` 和 `status.json` 修复。
修改代码、配置或 manifest 后重新调用，不能把旧 smoke 当作新实现已通过。

## 3. Deploy

调用 `run_experiments()`；同一个工具处理单任务、多种子和多阶段依赖。
现有队列自动发现 GPU、按用户设置一卡一个训练、遵守 `depends_on`、执行既有 OOM 重试并保存执行状态。
训练由 Python 执行器管理，等待不占模型 HTTP 请求超时；工具运行期间可从 workflow-progress.json、experiment-queue-state.json 和 experiment-runs 查看进度。
不要额外启动 bash/screen/nohup 调度器或第二个队列。

## 4. Collect and Return to Bridge

返回值包含任务完成数、日志和 deferred 项；具体数值以实际 JSON/CSV 为准。
读取这些产物，再回到 bridge 核对原计划、更新 tracker/coverage/EXPERIMENT_RESULTS.md。
缺后处理就补后处理 job，再 check/run；有 deferred 项就读取上游结果后展开或依据原计划门槛明确跳过。
续跑先检查已有回执；队列会复用完成且产物仍有效的任务，不默认重新训练。

环境、输入、预算或队列占用导致等待时，说明具体阻塞；实现错误在现有修复次数内定位日志、修复后重试。
方法性能不佳属于研究结果，不等于进程失败，不能通过修改 test 或伪造结果使其通过。
