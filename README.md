# Paper Agent 后端

用于研究项目的 Agent 工作流与 FastAPI 接口，包含阶段审阅、实验执行、论文图表、LaTeX 编译和知识库访问。

## 使用与技术文档

从 [平台模块文档](docs/README.md) 进入：[方案审阅](docs/guides/proposal-review.md)、[Skill 管理](docs/guides/skills.md)、[开发技术说明](docs/reference/skill-management.md) 和 [迭代记录](docs/CHANGELOG.md)。后续功能修改与文档在同一次 PR 中更新，见 [维护约定](docs/maintenance.md)。

## 安装与启动

需要 Python 3.11 或更新版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[web,test]"
cp .env.example .env
# 在 .env 中配置模型、密钥和数据路径
python -m paper_agent.api.app
```

工作流入口：`paper-workflow --help`。部署与接口说明见 [docs/web.md](docs/web.md)。

前端仓库：[paper-agent-frontend](https://github.com/Huang-Xinghao/paper-agent-frontend)。

本仓库仅包含后端源码、技能、测试和配置示例。研究项目、私有资源库、运行状态和真实密钥保存在服务器上。
