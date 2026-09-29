#!/usr/bin/env bash
# KPF 口语批改 skill · 环境安装
# 用法：bash scripts/setup.sh
# 可用环境变量覆盖：KPF_WHISPER_MODEL / KPF_PYTHON / KPF_HF_ENDPOINT
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SKILL_DIR"

MODEL="${KPF_WHISPER_MODEL:-large-v3-turbo}"
PYVER="${KPF_PYTHON:-3.12}"

# macOS 系统代理可能指向未启动的代理程序，会阻断 pip / HuggingFace 下载。
# 这里统一绕过代理；需要代理时用 KPF_USE_PROXY=1 显式开启。
if [[ "${KPF_USE_PROXY:-0}" != "1" ]]; then
  export NO_PROXY='*'
  export no_proxy='*'
fi

# 国内网络直连 huggingface.co 常失败，默认走镜像；有科学上网时用 KPF_HF_ENDPOINT= 清空
export HF_ENDPOINT="${KPF_HF_ENDPOINT-https://hf-mirror.com}"

command -v uv >/dev/null 2>&1 || {
  echo "缺少 uv。请先安装：brew install uv" >&2
  exit 1
}

# 坑：bash 在非 UTF-8 locale 下会把全角字符并入变量名，所以变量后面紧跟中文时必须写成 ${VAR}
echo "== 1/3 创建虚拟环境（Python ${PYVER}）"
uv venv --python "${PYVER}" .venv

echo "== 2/3 安装依赖"
uv pip install --python .venv/bin/python -r scripts/requirements.txt

echo "== 3/3 预下载 Whisper 模型：${MODEL}（首次约 1.6GB）"
.venv/bin/python - "$MODEL" <<'PY'
import sys
from faster_whisper import WhisperModel
name = sys.argv[1]
print(f"下载并加载 {name} …（仅首次需要下载）")
WhisperModel(name, device="cpu", compute_type="int8")
print("模型就绪。")
PY

echo
echo "安装完成。虚拟环境：$SKILL_DIR/.venv"
echo "模型缓存：~/.cache/huggingface"
