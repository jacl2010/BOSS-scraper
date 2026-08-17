#!/bin/zsh

set -u

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1

fail() {
  print -u2 "\n启动失败：$1"
  print -u2 "按回车键关闭窗口。"
  read -r
  exit 1
}

command -v uv >/dev/null 2>&1 || fail "未找到 uv，请先安装 uv >=0.12,<0.13。"
[[ -f uv.lock ]] || fail "未找到 uv.lock，请确认项目文件完整。"

UV_VERSION="$(uv --version 2>/dev/null | awk '{print $2}')"
[[ "$UV_VERSION" == 0.12.* ]] || fail "当前 uv 版本为 $UV_VERSION，需要 0.12.x。"

export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
print "正在检查本地依赖…"
uv sync --frozen || fail "依赖同步失败。请检查网络后重试。"

if [[ -z "${BOSS_MATCHER_LLM_API_KEY:-}" ]] && { [[ ! -f .env ]] || ! grep -q '^BOSS_MATCHER_LLM_API_KEY=' .env; }; then
  print "提示：尚未设置 BOSS_MATCHER_LLM_API_KEY。页面可以启动，但需设置后重新启动才能使用 LLM。"
fi

print "正在启动 http://127.0.0.1:8765 …"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

READY=0
for _ in {1..60}; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail "本地服务意外退出，请查看上方日志。"
  fi
  if uv run python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=1)" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.2
done

[[ "$READY" -eq 1 ]] || fail "15 秒内未检测到服务就绪。"

open "http://127.0.0.1:8765" || print "请手动打开 http://127.0.0.1:8765"
print "服务已就绪。关闭此窗口会停止本地服务。"
wait "$SERVER_PID"
