# 平台模块文档

本文档描述当前分支代码的能力，不代表线上服务已经部署。最近核对：2026-11-10。

## 用户：从这里开始

| 想完成的事情 | 阅读入口 |
| --- | --- |
| 理解、修改研究方案，再交给实验规划 | [方案审阅使用说明](guides/proposal-review.md) |
| 创建研究规则、选择适用阶段、管理版本 | [Skill 管理使用说明](guides/skills.md) |
| 添加外部查询服务、选择工具和阶段 | [MCP 工具使用说明](guides/mcp.md) |
| 理解研究 Agent 如何找论文、引用证据和呈现缺口 | [Wiki 检索使用说明](guides/wiki-retrieval.md) |
| 查看这轮更新和兼容性 | [迭代记录](CHANGELOG.md) |

## 开发者：理解实现与维护

| 问题 | 阅读入口 |
| --- | --- |
| MCP 如何发现、授权、调用并保护凭据 | [MCP 管理技术说明](reference/mcp-management.md) |
| 从完整调用链学习 MCP 工程取舍和面试场景 | [MCP 工程讲解与面试场景](explanations/mcp-engineering.md) |
| Skill 如何保存、选择、固定版本并交给 Agent | [Skill 管理技术说明](reference/skill-management.md) |
| 方案如何统一修订、校验、批准和交付 | [方案审阅技术说明](reference/proposal-review.md) |
| 论文如何清洗、保留原文位置、整理并核验证据 | [Wiki 预处理与核验](wiki.md#论文预处理与证据核验) |
| 混合检索、图导航、取证预算、引用检查及缓存如何实现 | [Wiki 检索技术说明](reference/wiki-retrieval.md) |
| 后端启动、部署与已有 API | [Web 服务](web.md)、[运行环境](runtime.md) |
| 前端组件、草稿和缓存如何处理 | [前端模块说明](https://github.com/Huang-Xinghao/paper-agent-frontend/blob/codex/idea-review/docs/modules.md) |
| 后续改功能时，哪些文档一起更新 | [文档维护约定](maintenance.md) |

跨仓链接当前指向 `codex/idea-review`。合并主分支时，两仓一起更新这些链接。

## 设计参考：与已实现功能区分

- [上游研究方案输出约定](proposal-output-contract-draft.md)：讨论稿，尚未实施。尤其“只有部分待决事项阻塞批准”仍是提案；当前只要修订结果存在待决问题，就不能批准。
- [Skill 挂载源码调研](skill-mounting-research-2026-11-05.md)：记录开源系统机制、本次取舍和来源，不替代操作说明。
- [MCP 开源实现调研与接入设计](mcp-research-2026-11-09.md)：比较协议、SDK、Agent 与平台实现，说明建议的接入范围、凭据和权限边界；保留调研时点的设计，当前实现范围以 MCP 使用与技术说明为准。
- 历史审计文档描述其记录时点，当前部署情况应通过服务状态和运行记录核对。
