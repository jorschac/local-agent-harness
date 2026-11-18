#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV="$HOME/.local/bin/uv"
else
  printf '%s\n' '未找到 uv。请安装 uv 后重试；构建不会在后台自行修改全局 Python。' >&2
  exit 1
fi
# Cache stays local; copy mode avoids macOS/filesystem reflink deadlocks.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.work/uv-cache}"
# Regular wheel install has no editable .pth dependency. UF_HIDDEN can be
# re-applied by macOS file-management/indexing; clearing it once is insufficient.
"$UV" sync --locked --no-editable --python 3.11 --link-mode copy --reinstall-package local-agent-harness
"$ROOT/.venv/bin/python" -c 'import harness; print("包导入通过:", harness.__file__)'
"$ROOT/.venv/bin/harness" --help >/dev/null
printf '%s\n' '构建/安装完成。无需 npm build，也无需先激活 venv。' './harness daemon status' './harness daemon start' './harness run "用工具计算 (35 × 7 - 128) ÷ (12 + 8)" --plugin arithmetic'
