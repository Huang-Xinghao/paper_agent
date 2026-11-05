# Skill 挂载与面向用户管理：源码调研

日期：2026-11-05。方法：下载固定提交的公开源码和官方规范，只读分析；没有部署这些系统，没有调用收费模型。此前方案保存在 `proposal-output-contract-draft.md`。用户随后授权实现侧边栏 Skill 管理，本报告先记录调研依据，再限定本次实现范围。

## 结论

Skill 是可复用的指令和资源包。保存、绑定、加载到模型上下文、准备执行资源、真正执行成功，是不同状态。管理页面必须让用户知道修改何时生效，而不能把“保存成功”展示成“模型已经使用”。

固定研究流水线适合直接加载本阶段必需 Skill，再叠加用户明确绑定的 Skill；通用助手更需要目录检索和按需激活。不能为了模仿通用助手，把已有确定阶段改成完全依赖模型自主选择。

权限应由运行器控制。`SKILL.md` 内的 `allowed-tools` 不会凭空新增工具或授权；审批研究方案也不等于授权运行任意脚本。

## 对照

| 系统 | 发现与挂载 | 更新与版本 | 面向用户的特点与代价 |
| --- | --- | --- | --- |
| Deep Agents | Middleware 扫描 backend 的多个 sources，目录进上下文，`read_file` 读正文；`pinned_skills` 可直接注入正文 | metadata 按 thread 保留，可显式刷新；sources 后项覆盖前项 | 框架组合灵活，但管理、发布、界面与租户权限需要应用自己补齐 |
| OpenClaw | 文件 roots 加 managed personal library；按平台、依赖、配置过滤；可搜索已装目录并读取 | 文件 roots 更新在下一 turn 生效；managed library 是不可变整包 revision，可按 session 保留精确版本 | 提供创建、导入、启停、就绪状态、回滚；更多状态需要解释，共享 Gateway 不是强租户隔离 |
| Magic / 小安公开实现 | 员工绑定 `skill_code`；system、workspace、personal 等来源；required preload 与可选目录并用 | provider 取包，staging 替换、backup 恢复、manifest 标识来源和版本；读 Skill 时可检查更新 | 业务绑定与执行服务分离，需同步；检查更新并不等于已经升级 |
| LobeHub | DB、built-in、project/device 来源；auto/manual、本轮选择和 pin；activate 后读取 reference 或准备资源 | 内容和 ZIP 资源有不同准备路径，pin 文本不能替代安装资源 | 能做普通用户能力卡和激活反馈，但云端、设备及资源路径需要统一优先级 |

## 重要实现细节

### Agent Skills 规范的边界

目录至少包含 `SKILL.md`，以 frontmatter 描述 name、description，可附 scripts、references、assets。宿主先给模型简短目录，再按需读取正文与资源。显式选择也可以由宿主直接注入，无需让模型再猜一次。规范中的 `allowed-tools` 属于实验字段，各宿主支持不同。

官方给出的目录 token 数只是经验估计，不能当成本项目实际耗时或费用。应针对相同任务比较未加载、旧版本、新版本的质量、耗时和 token。

### Deep Agents：pin 正文不等于冻结包版本

`SkillsMiddleware` 的 `pinned_skills` 表达保证正文在模型上下文中，不是内容 revision。metadata cache、正文内容和脚本版本不能混为一谈。当前提交的 `metadata.include_tools` 依赖应用注册工具或 resolver；外部包声明名字并不获得任意工具。

Code/CLI 层另有目录信任、canonical path 和 symlink 检查，不能把这些自动归因于所有 SDK 使用方式。

### OpenClaw：两种更新策略不能混说

普通文件目录的 watcher/safety rescan 发布新 snapshot，正在执行的 turn 使用旧 snapshot；managed personal library 则保存整包不可变 revision，包含文件内容、路径和 executable flag 的变化。旧 session 可以保留旧 revision，明确 refresh/attach 才切换。

这是工程取舍：及时看到编辑，还是保证长任务可复现。两者都可用，但 UI 必须说明“哪个任务用哪个版本”。其个人库 ownership 管发现和管理，文档明确仍是一个信任域，不能作为跨租户执行隔离的证明。

### 小安：绑定、安装和实际读取分开

已有本地资料中的“小安”对应 `dtyq/magic` 的公开代码。员工编辑 store 用 stable `skill_code` 绑定能力；runtime 再按来源查找、preload 或 read。`read_skills` 可以检查 provider 最新版本并提示，升级由另一条安装路径完成。

安装器先准备 staging，写 manifest，替换目录，必要时用 backup 恢复。manifest cache 失效仅代表安装元数据缓存更新，不代表历史上下文全部刷新；且该实现 manifest 写失败仅记录日志，不能把“安装返回成功”当成绝对的版本可追溯保证。

用户以前遇到的“自建 Skill 已更新，员工仍按旧内容工作”，与安装版本未同步的机制相符；没有当时线上日志，不能确定根因，也不能说公开部署与公司部署一致。私人库保存不等于公开上架。

### LobeHub：文字加载和资源挂载要成对处理

`skillEngineering.ts` 特意将带 `zipFileHash` 的 Skill 留在 available 目录，即使用户 pin，也不能只预注入正文。运行器还需要 activate 的记录去准备真实资源。否则模型知道“运行哪个脚本”，环境中却没有那个文件。

同一个名字的 catalog、正文和 reference 必须沿同一优先级解析，避免用户看见自定义 Skill，执行时却读到内置资源。共享访问场景通过 restricted manifest 裁掉执行类 API，并同步改 system prompt；仅在提示词里写限制不足以证明底层授权。

## To C 的工程取舍

1. 用名称、用途、适用阶段、来源、版本和启用状态表达能力；默认渲染正文，编辑时提供有标签的输入框。无需让用户理解服务器目录。
2. 创建先进入未绑定状态，明确选择阶段才自动附加。固定流程的必需能力继续存在，用户补充规则不能无意替换必需规则。
3. 新任务在每个阶段首次模型调用前固定正文及资源快照；该阶段内多轮调用及同一 run 的重试复用原快照，尚未开始的阶段取最新配置。升级前已有完整任务快照的 run 保留原策略。用户可回看个人修改历史并恢复，恢复也追加新版本。
4. 文本编辑可先交付完整闭环。ZIP、任意脚本、依赖安装和市场导入需要资源存储及执行环境，不能用一个上传按钮假装支持完整包。
5. 日志记录真正进入模型上下文的名称、版本、内容 hash；绑定列表不是使用证明。工具执行成功和最终研究质量仍是后续证据。
6. 当前项目是单账号工作台，Skill 放当前工作台管理库；不声称已有每用户隔离。未来做多用户时，所有读取、revision、绑定和资源访问都必须带 owner scope。

## 本项目现状与本次落地边界

调研时项目有 14 个 `SKILL.md`。`PhaseAgent.SKILL_BY_PHASE` 把阶段映射到必需 Skill，`_skill_prompt()` 读正文全文；工具权限由 `tools/profiles.py` 控制。`skill_resource_read` 当前主要支持 references/templates。方案修订的 `proposal_revision.py` 则是无工具单次模型调用，使用独立的代码内协议，原来未走 Skill loader。

用户随后明确需要侧边栏管理、创建、更新。本次采用：React 管理页 + FastAPI API + 现有 SQLite 管理库中的不可变 revision；内置内容保留在 Git，自定义内容和内置定制版保存在工作台数据目录。创建、编辑、预览、启停、阶段绑定与筛选、历史恢复、下载和恢复内置版接通；新任务按阶段首次调用固定正文和可读资源，真正加载时记录版本。配置作用域仍是工作台，所有项目共享，未实现项目独立的绑定配置。

内置发布与个人修改分开记录：`skills/release.json` 声明开发者发布号，每个 Skill 以正文、元数据和配套 references/templates 内容计算指纹；SQLite 归档当前工作台曾见过的内置发布。个人修订记录 `base_release`，继续使用对应基准的模板，内置更新不覆盖定制。恢复最新内置版会重新跟随发布，定制历史仍可恢复。旧定制未记录的基准明确显示未知；归档不等于完整 Git 发布史，也不覆盖外部数据和共享资料库。

附加 Skill 不改变阶段工具白名单、不取消专家批准、不改自动模式原流程。方案修订单次调用也只加载显式绑定到“方案修订”的文本，并保留结构校验与人工审批边界。本轮不安装开源系统、不实现 Skill 商店及外部脚本包运行，不自动把此前上游输出讨论稿变成已批准协议。

## 验证方法

重点验证 CRUD 与并发保存冲突、历史恢复、内置定制不覆盖源码、停用不再附加、阶段之间不串用、运行中版本不漂移、工具读取与正文版本一致、任务 worker 和方案修订接入。浏览器验证创建到刷新持久化、历史恢复、错误反馈、移动端和未保存修改保护。模型使用确定性替身验证接线，不以此宣称研究内容质量已通过真实模型验收。

2026-11-05 本地验收：Docker Linux 容器内后端相关测试 101 项通过；最后补充恢复跟随发布的断言及资源版本元数据后，Skill 与工具相关 35 项再次通过。独立测试服务上的 Skill 浏览器测试 5 项、原方案审阅回归 12 项通过。前端 TypeScript、改动文件 ESLint、生产构建及两仓 `git diff --check` 通过；构建仍提示已有的大体积 chunk。测试没有调用收费模型，也没有写入线上服务器；研究语义质量仍需后续真实任务比较。Skill 管理入口为 `/skills`，用户选择适用阶段后保存，尚未开始的阶段会在首次模型调用前固定最新正文与内置可读资源；已开始阶段继续使用原快照。

## 固定提交与可复查来源

- [Agent Skills specification](https://github.com/agentskills/agentskills/blob/83021e2be457a9b6679148719c1f6074f0ad975e/docs/specification.mdx)、[宿主接入](https://github.com/agentskills/agentskills/blob/83021e2be457a9b6679148719c1f6074f0ad975e/docs/client-implementation/adding-skills-support.mdx)。
- [Deep Agents middleware](https://github.com/langchain-ai/deepagents/blob/5ed6d1c32f060143070b32826f87d2f83e569088/libs/deepagents/deepagents/middleware/skills.py)、[CLI trust](https://github.com/langchain-ai/deepagents/blob/5ed6d1c32f060143070b32826f87d2f83e569088/libs/code/deepagents_code/skills/trust.py)。
- [OpenClaw skills](https://github.com/openclaw/openclaw/blob/8a34fd4688c9e43a583e281134149f4e728fce98/docs/tools/skills.md)、[personal library](https://github.com/openclaw/openclaw/blob/8a34fd4688c9e43a583e281134149f4e728fce98/docs/tools/skill-workshop/personal-library.md)、[library RPC](https://github.com/openclaw/openclaw/blob/8a34fd4688c9e43a583e281134149f4e728fce98/src/gateway/server-methods/skills-library.ts)。
- [Magic prompt](https://github.com/dtyq/magic/blob/0f1432c329ff4010df86252b963a32795e23d090/backend/super-magic/app/core/skill_utils/prompt.py)、[read/update](https://github.com/dtyq/magic/blob/0f1432c329ff4010df86252b963a32795e23d090/backend/super-magic/app/tools/read_skills.py)、[installer](https://github.com/dtyq/magic/blob/0f1432c329ff4010df86252b963a32795e23d090/backend/super-magic/app/core/skill_utils/installer.py)、[员工绑定](https://github.com/dtyq/magic/blob/0f1432c329ff4010df86252b963a32795e23d090/frontend/magic-web/src/pages/superMagic/pages/CrewEdit/store/skills-store.ts)。旧案例另核对 f9973c5cfe5d668867b1a8dc5c50e4be5081ee56，不当作当前生产状态。
- [LobeHub skillEngineering](https://github.com/lobehub/lobehub/blob/6b4bed2e1eee52018f4e51342735550951fd8435/src/services/chat/mecha/skillEngineering.ts)、[ExecutionRuntime](https://github.com/lobehub/lobehub/blob/6b4bed2e1eee52018f4e51342735550951fd8435/packages/builtin-tool-skills/src/ExecutionRuntime/index.ts)、[restricted manifest](https://github.com/lobehub/lobehub/blob/6b4bed2e1eee52018f4e51342735550951fd8435/packages/builtin-tool-skills/src/resolveRestrictedManifest.ts)。

本地原始文件位于两个业务仓库之外的 `research-cache/skill-mounting-20261105`。另尝试 pi-mono 时 GitHub API 返回 403，没有纳入比较结论。以上属于对应 commit 的源码事实，不能直接推断线上配置、账号权限或实测性能。
