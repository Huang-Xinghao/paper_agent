---
name: run-experiment
description: "在本地、远程、 Vast.ai 或 Modal 无服务器 GPU 上部署和运行 ML 实验。当用户说“运行实验”、“部署到服务器”、“运行实验”或需要启动作业时使用。"
argument-hint: "[experiment-description]"
allowed-tools: Bash(*), Read, Grep, Glob, Edit, Write, Skill(serverless-modal)
---

# 运行实验

部署并运行 ML 实验：$ARGUMENTS

## 工作流程

### 第1步：检测环境

读取项目的`CLAUDE.md`确定实验环境：

- **本地 GPU** ( `gpu: local` ): 查找本地 CUDA/MPS 设置信息
- **远程服务器** ( `gpu: remote` )：查找 SSH 别名、conda env、代码目录
- **Vast.ai** ( `gpu: vast` )：在项目根目录检查 `vast-instances.json` — 如果存在正在运行的实例，则使用它。另请检查 `CLAUDE.md` 中的 `## Vast.ai` 部分。
- **Modal** ( `gpu: modal` )：通过 Modal 的无服务器 GPU 。没有 SSH，没有 Docker，自动缩放到零。委托给 `/serverless-modal` 。

**Modal 检测：** 如果 `CLAUDE.md` 有 `gpu: modal` 或 `## Modal` 部分，则整个部署由 `/serverless-modal` 处理。跳转至**步骤 4：部署 ( Modal )** — 不需要步骤 2-3（Modal 自动处理代码同步和 GPU 分配）。

**环境合约** ( `../shared-references/compute-env-contract.md` )：在构建或信任任何环境之前，请阅读提供商的环境账本 ( `.aris/compute/<provider>.md` ) - 未更改的规范哈希意味着热重用，更改后意味着重建。新环境 → 首先编写声明性规范，针对此提供程序的形状呈现它，并且永远不要仅在导入成功时声明它已准备好：运行种子内核见证，并在任何 rebuild/doc 编辑之后运行代理遵循文档传递（新的子代理逐字执行记录的调用并报告文档与现实差异）。

**Vast.ai 检测优先级：**
1. 如果 `CLAUDE.md` 具有 `gpu: vast` 或 `## Vast.ai` 部分：
   - 如果 `vast-instances.json` 存在并且有一个正在运行的实例 → 使用该实例
   - 如果没有正在运行的实例 → 调用 `/vast-gpu provision` 来分析任务，提供成本优化的 GPU 选项，并租用用户的选择
2. 如果 `CLAUDE.md` 中没有找到服务器信息，请询问用户。

### 第 2 步：运行前检查

检查目标机器上的 GPU 可用性：

**远程（SSH）：**
```bash
ssh <server> nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
```

**远程（Vast.ai）：**
```bash
ssh -p <PORT> root@<HOST> nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
```
（从 `vast-instances.json` 读取 `ssh_host` 和 `ssh_port` ，或运行 `vastai ssh-url <INSTANCE_ID>` 返回 `ssh://root@HOST:PORT` ）

**本地：**
```bash
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
# 或对于 Mac MPS：
python -c "import torch; print('MPS available:', torch.backends.mps.is_available())"
```

可用 GPU = 已用内存 < 500 MiB。

### 第 3 步：同步代码（仅限远程）

检查项目的 `CLAUDE.md` 中的 `code_sync` 设置。如果未指定，则默认为 `rsync` 。

#### 选项 A：rsync（默认）

仅同步必要的文件 - 不同步数据、检查点或大文件：
```bash
rsync -avz --include='*.py' --exclude='*' <local_src>/ <server>:<remote_dst>/
```

#### 选项 B：git （当 CLAUDE.md 设置为 `code_sync: git` 时）

将本地更改推送到远程存储库，然后拉取服务器：
```bash
# 1.本地推送
git add -A && git commit -m "sync: experiment deployment" && git push

# 2. 拉取服务器
ssh <server> "cd <remote_dst> && git pull"
```

优点：版本跟踪、一键多服务器同步、无需 rsync include/exclude 规则。

#### 选项 C：Vast.ai 实例

将代码同步到 vast.ai 实例（始终 rsync，代码目录为 `/workspace/project/` ）：
```bash
rsync -avz -e "ssh -p <PORT>" \
  --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' \
  --include='*.txt' --include='*.sh' --include='*/' \
  --exclude='*.pt' --exclude='*.pth' --exclude='*.ckpt' \
  --exclude='__pycache__' --exclude='.git' --exclude='data/' \
  --exclude='wandb/' --exclude='outputs/' \
  ./ root@<HOST>:/workspace/project/
```

根据 env 合约安装依赖项（有序阶段 — 首先引脚，每个阶段一个 `pip install`；请参阅 `../shared-references/compute-env-contract.md` ）：
```bash
ssh -p <PORT> root@<HOST> "pip install -q torch==<pinned>"       # 第一阶段：引脚
ssh -p <PORT> root@<HOST> "pip install -q <remaining packages>"  # 阶段 2+
```
旧版回退 — 仅 `requirements.txt`，无环境规范：作为一个阶段安装，并将任何版本冲突视为转换为有序阶段的信号：
```bash
scp -P <PORT> requirements.txt root@<HOST>:/workspace/
ssh -p <PORT> root@<HOST> "pip install -q -r /workspace/requirements.txt"
```

### 步骤3.5：W&B集成（当`wandb: true`位于CLAUDE.md时）

**如果 `wandb` 未设置或者 `false` 位于 CLAUDE.md 中，则完全跳过此步骤。**

部署之前，请确保实验脚本具有 W&B 日志记录：

1. **检查 wandb 是否已在脚本中** — 查找 `import wandb` 或 `wandb.init` 。如果存在，请跳至步骤 4。

2. **如果不存在，请将 W&B 日志记录**添加到训练脚本中：
   ```python
   import wandb
   wandb.init(project=WANDB_PROJECT, name=EXP_NAME, config={...hyperparams...})

   # 训练循环内部：
   wandb.log({"train/loss": loss, "train/lr": lr, "step": step})

   # 评估后：
   wandb.log({"eval/loss": eval_loss, "eval/ppl": ppl, "eval/accuracy": acc})

   # 最后：
   wandb.finish()
   ```

3. **要记录的指标**（添加适用于实验的内容）：
   - `train/loss` — 每步训练损失
   - `train/lr` — 学习率
   - `eval/loss` 、 `eval/ppl` 、 `eval/accuracy` — 每个时期的评估指标
   - `gpu/memory_used` — GPU 存储器（通过 `torch.cuda.max_memory_allocated()` ）
   - `speed/samples_per_sec` — 吞吐量
   - 实验已计算的任何自定义指标

4. **验证目标机器上的wandb登录：**
   ```bash
   ssh <server> "wandb status"  # 应该显示已登录
   # 如果没有登录：
   ssh <server> "wandb login <WANDB_API_KEY>"
   ```

> W&B 项目名称和 API 密钥来自 `CLAUDE.md`（请参见下面的示例）。实验名称是根据脚本名称+时间戳自动生成的。

### 第 4 步：部署

#### 远程（通过 SSH + screen ）

对于每个实验，使用 GPU 绑定创建专用的 screen 会话：
```bash
ssh <server> "screen -dmS <exp_name> bash -c '\
  eval \"\$(<conda_path>/conda shell.bash hook)\" && \
  conda activate <env> && \
  CUDA_VISIBLE_DEVICES=<gpu_id> python <script> <args> 2>&1 | tee <log_file>'"
```

#### Vast.ai实例

不需要 conda——Docker 镜像有环境。使用 `/workspace/project/` 作为工作目录：
```bash
ssh -p <PORT> root@<HOST> "screen -dmS <exp_name> bash -c '\
  cd /workspace/project && \
  CUDA_VISIBLE_DEVICES=<gpu_id> python <script> <args> 2>&1 | tee /workspace/<log_file>'"
```

启动后，更新此实例的 `vast-instances.json` 中的 `experiment` 字段。

#### Modal（无服务器）

当检测到 `gpu: modal` 时，委托给 `/serverless-modal` ：

1. **分析任务** — 确定 VRAM 需求，选择 GPU ，估算成本
2. **生成启动器** — 创建一个 `modal_launcher.py` 来包装训练脚本，使用 `modal.Mount.from_local_dir` 作为代码，使用 `modal.Volume` 作为结果
3. **运行** — `modal run modal_launcher.py`（本地运行，GPU远程执行）
4. **收集结果** — 结果通过 Volume 或 stdout 返回，无需手动下载

`CLAUDE.md` 的关键 Modal 设置：
- `modal_gpu` ：GPU 覆盖（默认：根据 VRAM 分析自动选择）
- `modal_timeout` ：最大秒数（默认值：21600 = 6 小时）
- `modal_volume` ：用于持久结果的命名卷

无需 SSH，无需代码同步，无需 screen 会话。  Modal 处理一切。

#### 当地的

```bash
# 带有 CUDA 的 Linux
CUDA_VISIBLE_DEVICES=<gpu_id> python <script> <args> 2>&1 | tee <log_file>

# 带 MPS 的 Mac（PyTorch 自动使用 MPS）
python <script> <args> 2>&1 | tee <log_file>
```

对于本地长时间运行的作业，请使用 `run_in_background: true` 来保持对话响应。

### 第 5 步：验证启动

**远程（SSH）：**
```bash
ssh <server> "screen -ls"
```

**远程（Vast.ai）：**
```bash
ssh -p <PORT> root@<HOST> "screen -ls"
```

**Modal :**
```bash
modal app list         # 检查应用程序是否正在运行
modal app logs <app>   # 流日志
```

**本地：** 检查进程正在运行并且 GPU 已分配。

### 第6步：飞书通知（如果配置）

部署验证后，检查 `~/.claude/feishu.json` ：
- 发送 `experiment_done` 通知：启动了哪些实验、哪些 GPU、预计时间
- 如果配置不存在或模式 `"off"` ：完全跳过（无操作）

### 步骤7：自动销毁Vast.ai实例（当`gpu: vast`和`auto_destroy: true`时）

**如果不使用vast.ai或`auto_destroy`是`false`则跳过此步骤。**

实验完成后（通过`/monitor-experiment`或screen会话结束检测）：

1. **从实例下载结果**：
   ```bash
   rsync -avz -e "ssh -p <PORT>" root@<HOST>:/workspace/project/results/ ./results/
   ```

2. **下载日志**：
   ```bash
   scp -P <PORT> root@<HOST>:/workspace/*.log ./logs/
   ```

3. **销毁实例**以停止计费：
   ```bash
   vastai destroy instance <INSTANCE_ID>
   ```

4. **更新 `vast-instances.json`** — 将状态标记为 `destroyed` 。

5. **报告费用**：
   ```
   Vast.ai 实例 <ID> 自动销毁。
   - 持续时间：~X.X 小时
   - 预计成本：~ $X .XX
   - 结果保存至：./results/
   ```

> 这可确保用户永远不会因空闲实例而付费。当`auto_destroy: true`（默认）时，整个生命周期是自动的：租赁→设置→运行→收集→销毁。

## 主要规则

- 始终首先检查 GPU 可用性 - 切勿盲目分配 GPU（Modal 除外，它自动管理分配）
- 每个实验都有自己的 screen 会话 + GPU（远程）或后台进程（本地）
- 使用`tee`保存日志以供以后检查
- 使用 `run_in_background: true` 运行部署命令以保持对话响应
- 回报：which GPU ，which screen/process ，什么命令，预计时间
- 如果进行多个实验，请在不同的 GPU 上并行启动它们
- **Vast.ai 成本意识**：使用 `gpu: vast` 时，始终报告运行成本。如果是 `auto_destroy: true` ，则在所有实验完成后立即销毁该实例
- **Modal 成本意识**：运行前始终估算并显示成本。  Modal 自动缩放为零 — 无空闲计费，无需手动清理

## CLAUDE.md 示例

用户应该将服务器信息添加到项目的 `CLAUDE.md` 中：

```markdown
## 远程服务器
- gpu:remote #使用预先配置的SSH服务器
- SSH：`ssh my-gpu-server`
- GPU：4x A100（每个 80GB）
- 康达：`eval "$(/opt/conda/bin/conda shell.bash hook)" && conda activate research`
- 代码目录：`/home/user/experiments/`
- code_sync: rsync # 默认。或者设置为“git”以获取 git push/pull 工作流程
- wandb: false # 设置为“true”以自动将 W&B 日志记录添加到实验脚本中
- wandb_project: my-project # W&B 项目名称（如果 wandb: true 则为必需）
- wandb_entity: my-team # W&B team/user（可选，如果省略则使用默认值）

## Vast.ai
- GPU：巨大 # 按需租用 GPU 来自 vast.ai
- auto_destroy: true # 实验完成后自动销毁（默认值：true）
- max_budget: 5.00 # 可选：每个实验花费的最大总金额

## Modal
- gpu: modal # serverless GPU via Modal（无 SSH，自动缩放至零）
- modal_gpu: A100-80GB # 可选：覆盖 GPU 选择（默认：自动选择）
- modal_timeout: 21600 # 可选：最大秒数（默认值：6 小时）
- modal_volume: my-results # 可选：用于结果持久化的命名卷

## 当地环境
- gpu: local # 使用本地 GPU
- Mac MPS / Linux CUDA
- Conda 环境：`ml`（Python 3.10 + PyTorch）
```

> **Vast.ai 设置**：运行 `pip install vastai && vastai set api-key YOUR_KEY` 。在 https://cloud.vast.ai/manage-keys/. 上传您的 SSH 公钥 在您的 `CLAUDE.md` 中设置 `gpu: vast` — `/run-experiment` 将自动租用一个实例，运行实验，并在完成后销毁它。

> **Modal 设置**：运行 `pip install modal && modal setup` 。在 https://modal.com/settings 绑定支付方式（切勿通过 CLI）以解锁完整的 $30/month 免费套餐（无卡：仅限 $5/month）。设置工作空间支出限额以防止意外收费。在 `CLAUDE.md` 中设置 `gpu: modal` — 非常适合没有本地 GPU 且需要调试代码或运行小规模测试的用户。

> **W&B 设置**：在服务器上运行 `wandb login` 一次（或设置 `WANDB_API_KEY` 环境变量）。该技能从 CLAUDE.md 读取 project/entity 并自动将 `wandb.init()` + `wandb.log()` 添加到您的训练脚本中。仪表板：`https://wandb.ai/<entity>/<project>`。

