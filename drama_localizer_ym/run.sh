#!/usr/bin/env bash
# 启动包装脚本：环境由 systemd 的 EnvironmentFile 加载
# 放在正式运行目录，systemd 单元 ExecStart 指向它
set -euo pipefail
umask 027
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${CUTLY_RUN_DIR:-$ROOT_DIR/drama_localizer}"
# 运行副本位于 drama_localizer/ 时，模型在同级的 drama_localizer_ym/models；
# 直接从源码目录启动时则回退到源码目录自身的 models/。
if [ -d "$ROOT_DIR/drama_localizer_ym/models" ]; then
  MODEL_DIR="$ROOT_DIR/drama_localizer_ym/models"
else
  MODEL_DIR="$SCRIPT_DIR/models"
fi
cd "$RUN_DIR"
# 模型随源码管理目录迁移，运行时通过环境变量读取，不依赖用户家目录缓存。
export HF_HOME="$MODEL_DIR/huggingface"
export TORCH_HOME="$MODEL_DIR/torch"
export XDG_CACHE_HOME="$MODEL_DIR/cache"
exec "$RUN_DIR/venv/bin/python" -m uvicorn workstation:app --host 0.0.0.0 --port 8000
