---
name: run-experiment
description: experiment-bridge 内部使用的本地执行检查与故障诊断子 skill，用于检查实际输入、定位日志并将实现错误交回 bridge。
---

# 实验执行检查与故障诊断

由 experiment-bridge 调用，不作为独立 workflow 节点。执行接口以运行器注入的说明为准。

运行前核对实际输入、命令参数、输出目录和依赖是否与代码一致；缺代码或配置时交回 bridge 补齐。

执行后读取工具返回的错误和日志路径，按需检查 `experiment-runs/<attempt>/output.log`、`status.json`。
环境、输入、预算或队列占用导致等待时，说明具体阻塞；代码实现错误交回 bridge 修复。

将实际日志和产物位置交回 bridge，由其解释结果并核对完整计划。
方法性能不佳属于研究结果，不等于进程失败；不能篡改测试或伪造结果使其通过。
