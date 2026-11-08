#!/bin/sh
# 在挂载盘创建独立的 CPU 解析环境，避免修改应用依赖或把模型写入容器层。
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime=${1:-"$repo_root/.paper-agent/mineru-runtime"}
case "$runtime" in /*) ;; *) echo 'Runtime path must be absolute.' >&2; exit 1 ;; esac
mkdir -p "$runtime/tmp" "$runtime/cache" "$runtime/home"
export TMPDIR="$runtime/tmp"
export PIP_CACHE_DIR="$runtime/cache"
export XDG_CACHE_HOME="$runtime/cache"
export HF_HOME="$runtime/cache/huggingface"
export MODELSCOPE_CACHE="$runtime/cache/modelscope"
export MINERU_HOME="$runtime/home"
export MINERU_MODEL_SMALL_BACKEND=onnx
export MINERU_MODEL_ONNX_DEVICE=cpu
export MINERU_MODEL_VLM_ENGINE=llama-cpp
export MINERU_LLAMA_CPP_BACKEND=cpu
export MINERU_MODEL_SOURCE=local

if [ ! -x "$runtime/venv/bin/python" ]; then
    python3 -m venv "$runtime/venv"
fi
"$runtime/venv/bin/python" -m pip install --no-compile 'mineru==4.1.7'
"$runtime/venv/bin/python" -m pip check
"$runtime/venv/bin/mineru-kit" models download --tier standard --small-backend onnx --vlm-engine llama-cpp --source "${MINERU_DOWNLOAD_SOURCE:-modelscope}"
"$runtime/venv/bin/mineru-kit" models verify --tier standard --small-backend onnx --vlm-engine llama-cpp

# Docker exec --env-file 读取此文件；只在环境与模型校验通过后发布。
cat > "$runtime/runtime.env.tmp" <<EOF
PAPER_AGENT_MINERU_PYTHON=$runtime/venv/bin/python
MINERU_HOME=$MINERU_HOME
MINERU_MODEL_SMALL_BACKEND=onnx
MINERU_MODEL_ONNX_DEVICE=cpu
MINERU_MODEL_VLM_ENGINE=llama-cpp
MINERU_LLAMA_CPP_BACKEND=cpu
MINERU_MODEL_SOURCE=local
TMPDIR=$TMPDIR
XDG_CACHE_HOME=$XDG_CACHE_HOME
HF_HOME=$HF_HOME
MODELSCOPE_CACHE=$MODELSCOPE_CACHE
EOF
mv "$runtime/runtime.env.tmp" "$runtime/runtime.env"
echo "Ready: $runtime/runtime.env"
