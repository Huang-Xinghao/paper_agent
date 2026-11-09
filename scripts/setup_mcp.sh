#!/usr/bin/env bash
# 在挂载目录安装小型运行依赖；不重建镜像、不下载模型。
set -euo pipefail
runtime_root="${1:-/review/paper_agent/.paper-agent/mcp-runtime}"
mkdir -p "$runtime_root"
runtime_root="$(cd "$runtime_root" && pwd)"
python -m pip install --target "$runtime_root/packages" --upgrade --no-cache-dir \
  'mcp==2.3.0' 'httpx2==2.16.0' 'httpcore2==2.16.0' 'cryptography>=43,<47' 'jsonschema>=4.20,<5'
# 凭据密钥独立于 SQLite；备份和迁移必须同时保留它，不能只搬数据库。
export MCP_SETUP_ROOT="$runtime_root"
PYTHONPATH="$runtime_root/packages${PYTHONPATH:+:$PYTHONPATH}" python - <<'PY'
import os
from pathlib import Path
from cryptography.fernet import Fernet
root = Path(os.environ['MCP_SETUP_ROOT'])
key_dir = root.parent / 'mcp-credentials'
key_dir.mkdir(mode=0o700, exist_ok=True)
key = key_dir / 'credential.key'
if not key.exists():
    descriptor = os.open(str(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(Fernet.generate_key())
Fernet(key.read_bytes().strip())
env_file = root / 'runtime.env'
extras = [line for line in env_file.read_text(encoding='utf-8').splitlines() if line.startswith('PAPER_AGENT_MCP_TRUSTED_ENDPOINTS=')] if env_file.exists() else []
env_file.write_text(f'PYTHONPATH={root / "packages"}\nPAPER_AGENT_MCP_CREDENTIAL_KEY_FILE={key}\n' + ''.join(line + '\n' for line in extras), encoding='utf-8')
print('MCP 依赖与加密配置已准备。密钥未输出；重启 API 后生效。')
PY
