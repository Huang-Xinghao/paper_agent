# Web 工作台服务层

前端独立位于 `/home/xinghaohuang/paper-agent-web`，不加入 Python 包目录。完整操作说明、开发命令和部署示例见该目录的 README.md 与 deploy/。

## 启动

```bash
/data/apps/micromamba/envs/xinghao/bin/python -m pip install -e '.[web,test]'
./run-web-api
```

仅监听 `127.0.0.1:8000`。前端开发代理 `/api` 到该地址。API 文档为 `/api/docs`，OpenAPI 为 `/api/openapi.json`。

环境变量：

| 变量 | 默认 / 用途 |
| --- | --- |
| `PAPER_AGENT_API_PORT` | 8000 |
| `PAPER_AGENT_RUNTIME_PYTHON` | 启动脚本默认使用 `/data/apps/micromamba/envs/xinghao/bin/python` |
| `PAPER_AGENT_WEB_DATA` | 仓库 `.paper-agent/web`，保存 SQLite、Web 运行日志与管理元数据 |
| `PAPER_AGENT_PROJECT_ROOTS` | 用系统路径分隔符分隔的项目根目录；默认包含 `/data/xinghaohuang/paper-agent-projects` 和管理目录下的 `projects` |

仅允许登记这些根目录下的项目，路径解析后校验，支持已有项目的真实目录。不会通过 API 暴露项目根目录外的符号链接目标或隐藏文件。

## 模块边界

- `api/app.py`：项目、文件、运行、审阅、日志与 SSE 接口；Pydantic 输入校验。
- `api/store.py`：SQLite WAL 管理存储、事件记录和运行状态更新。
- `api/worker.py`：脱离 API 生命周期的监控进程及工作流执行子进程。
- `api/runtime.py`：遵循现有启动器的 CUDA 计算检查，失败时不回退 CPU。
- `agent/workflow_cli.py`：增加可选 `on_event` 回调，其余调用行为保持原样。Web 使用它持久化节点和工具事件，CLI 不传入时不依赖 API。

项目内原有 `workflow-state.json`、`workflow-progress.json`、实验回执和报告仍为研究事实来源；不在数据库复制一套可编辑的科学状态。数据库记录 Web 请求、运行生命周期和操作历史。

## 运行约束

新项目的 `start` 请求先判断意图：身份和问候直接回答，其他消息由无工具权限的模型结合最近 8 轮普通对话判断。普通知识问答返回 `200 {intent: "chat", reply}`，目标不明确返回 `200 {intent: "clarify", reply}`；回复作为 `conversation_turn` 事件持久化，不创建运行或工作流状态。明确研究请求合并上下文目标后创建运行并返回 `202 {id}`。判断失败返回 503，绝不自动启动研究。已有工作流的恢复和专家审阅维持原有语义。

独立监控进程负责启动模型/实验进程、记录运行结果及中断。SQLite 事务和唯一索引阻止 Web 重复启动同一项目，首版全局只放行一个 Web 项目，避免跨项目争用 GPU。监控进程另外持有项目文件锁，界面编辑在排队、运行和停止阶段均被拒绝。

Web 专家反馈携带原审阅问题与用户回答，使用 `advance_after_revision=True`：修订当前节点后复用产物确认校验和版本摘要记录，成功即进入下游，不再次生成同一节点的问题。下游节点仍按专家模式暂停；显式运行终点和最终节点仍停止，修订或产物校验失败不会进入下游。终端 `--revise` 的默认再次审阅语义保持不变。

终端进程识别使用已登记路径及 CLI 的 `--project` 参数。Web 不接管既有终端进程，也不能阻止用户随后在终端启动另一个任务。不要同时使用 Web 和 CLI 修改同一项目。

文件保存使用 SHA256 乐观并发校验及原子替换；专家提交校验检查点和当前相关文件哈希，工作流实际开始前再次校验。报告内容允许人工编辑，JSON 状态文件及执行回执不提供界面编辑入口。

SSE 使用数据库自增事件 ID，支持 `Last-Event-ID` 和 `after` 补发，自动心跳；页面同时提供定时刷新。前端关闭或 API 正常重启不停止独立进程。服务进程遗失 / 机器重启后只标记中断，不自动重跑真实实验。停止操作使用进程树发送中断，超时后清理仍存活进程；恢复基于已有节点与有效回执。

## 验证

```bash
/data/apps/micromamba/envs/xinghao/bin/python -m pytest tests/test_web_api.py tests/test_workflow_cli.py tests/test_expert_checkpoint.py -q
```

Web 测试使用临时根目录，并替换模型执行，覆盖发现已有项目、路径与符号链接边界、文件冲突、运行互斥、停止请求、失联恢复、审阅版本校验及事件持久化。

这是本地单用户版本；没有登录、多租户、共享 GPU 全局调度、Wiki Web 编辑或公开访问配置。部署模板仅供准备，未自动安装 systemd 服务或启动 nginx。

## 本次验证记录

- Web/API/CLI/专家检查点相关测试：66 passed。
- 全量后端测试：423 passed，1 failed。失败为 `tests/test_data_search.py::test_literature_data_search_ideas_handoff`，要求 ideas 提示词包含 `data-search/DATA_REPORT.md`；当前未修改的工作流已移除这段注入，与本次 Web 服务层不在同一路径。本次未为了通过旧断言修改研究工作流。
- 前端 TypeScript、ESLint、Prettier 与生产构建通过；6 项隔离浏览器测试覆盖项目列表/搜索、项目创建、报告编辑与保存、专家提交版本令牌、手机文件导航、请求失败重试。
- 未运行真实模型请求或 GPU 实验。已有研究项目仅用于只读浏览，浏览器写入验证使用临时项目目录。

## 公网单用户部署（2026-10-24）

`www.xora.top` 在 Vercel 提供前端；同源 `/api/` 转发到 `api.xora.top`，后者通过专用 Cloudflare Tunnel 连接本机 API。DNS 已由 Cloudflare 管理，域名仍在阿里云注册续费。

登录配置位于 `~/.config/paper-agent/web.env`，设置 `PAPER_AGENT_ADMIN_USER`、`PAPER_AGENT_PASSWORD_HASH`（scrypt）、`PAPER_AGENT_PUBLIC_ORIGINS` 和 `PAPER_AGENT_PUBLIC_HOSTS`。配置任何公网 Host 而不提供密码摘要时，启动会失败。API 继续仅监听回环地址，Tunnel 将 Host 重写为 `localhost`。本地开发不配置密码时沿用原先本地访问方式；切勿将此模式用于公网。

会话使用数据库持久化的随机令牌摘要，12 小时失效；浏览器 Cookie 使用 Secure、HttpOnly、SameSite=Lax。登录最多每分钟 10 次；写请求需要合法 Origin，所有研究 API、文档、下载和事件流需要会话。会话退出立即撤销，已打开的事件流最多 15 秒后关闭。修改密码后应清空 `web_sessions` 撤销已有会话。不要将 `PAPER_AGENT_COOKIE_SECURE=0` 用于公网。

服务为 `systemctl --user` 管理的 `paper-agent-api.service` 与 `paper-agent-tunnel.service`。已启用用户 linger，在注销后继续运行并随用户管理器开机启动。API 服务保留 `KillMode=process`，重启不主动终止分离的研究进程。

凭据、Tunnel 配置及初始登录信息位于所有者权限保护的 `~/.config/paper-agent/` 和 `~/.cloudflared/`，不在代码仓库内。`~/.cloudflared/cert.pem` 为管理凭据，权限范围大于单个 Tunnel；保管好它。隧道服务实际使用专用 JSON 凭据。

### 协作方式切换

Web 的 `resume` 请求现在保留显式传入的 `mode`，交由现有 CLI 节点恢复入口应用；不传时继续沿用已有模式。已有待审阅检查点仍必须通过 `review` 提交，不允许绕过检查点切换模式。页面顶部新增待审阅问题跳转与协作方式设置，问答只在真实 `awaiting_user` 检查点出现。

### 对话与产物工作区（2026-10-25）

前端采用左侧研究对话、右侧产物工作区。对话中的研究目标、补充指令和专家反馈分别提交既有 `start`、`resume`、`review` 请求；审阅仍携带检查点令牌，运行期间禁止编辑产物。旧项目从已有阶段记录显示产物，不推测历史模型回复。

`GET /api/projects/{id}/activity` 返回 `{events, has_more}`，默认按时间正序返回最近 100 条，可用 `before` 向前翻页或 `after` 向后增量恢复，两者不能同时使用；`limit` 为 1–500。事件保持数据库 ID 与项目隔离；API 登录保护同其他研究接口。浏览器先加载最近事件，再通过带 `after` 的 SSE 接收增量，每 5 秒轮询补齐，加载较早记录不跳动当前滚动位置。

Agent 在每轮模型请求完成后记录 `assistant_output`（公开回复前 20,000 字符）；`tool_start` 携带 `call_id` 和参数文本前 6,000 字符，`tool_end` 携带调用 ID、结果文本前 6,000 字符及截断标记。前端可展开工具详情，公开回复以 Markdown 渲染，不展示 `<think>` 私有推理内容。当前模型调用仍按轮次返回，不提供逐 token 输出；只有新启动的进程会产生新增事件，旧运行无法补录未保存的输出。完整研究内容以项目产物为准。


## 电池范式库与电池资源库

登录后可通过 `GET /api/libraries/{library}` 浏览目录，`library` 为 `paradigms`（电池范式库）或 `battery`（电池资源库）。参数 `q` 对标题、路径和正文进行分词匹配，`category` 筛选顶层目录；`limit` 最大 1000，响应包括分类计数、总数及截断标记。

`GET /api/libraries/{library}/document?path=...` 返回 Markdown 正文与元信息，隐藏 YAML frontmatter；`GET /api/libraries/{library}/asset?path=...` 提供 PNG/JPEG/WebP/GIF/PDF。接口沿用会话登录保护，仅提供读取，拒绝隐藏路径、目录越界和符号链接。单篇文档限制 2 MiB，最多扫描 5000 篇。

电池范式库使用现有 `PAPER_AGENT_WIKI_ROOT`，默认仓库的 `research-wiki`；电池库可通过 `PAPER_AGENT_BATTERY_ROOT` 配置，默认仓库的 `电池资源库`。刷新页面会重新读取文件，无需导入数据库。验证：`python -m pytest tests/test_web_library.py tests/test_web_api.py -q`。

`GET /api/libraries/paradigms/graph` 返回具有 `node_id` 的 Wiki 条目与已记录的关系，节点携带文档路径与任务/方法等标签；仅保留两端存在的关系，忽略重复、无效和缺失端点的连线，不根据标签推测研究关系。前端默认显示交互图谱，点击节点后读取原有文档接口。

`GET /api/libraries/paradigms/figures` 返回已归档的论文图片，包含库内图片路径、论文名、可用的论文文档路径、图号、PDF 页码与原始图注。图片位于 `figures/`，同名 `.md` 使用 `Source: paper:<id>`、`PDF page:` 与可选 `Paper title:` 元信息，剩余正文作为图注。缺失图注不推断生成，隐藏文件与符号链接不读取，最多返回 1000 张。图片读取沿用登录保护的 asset 接口。
