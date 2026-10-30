#!/usr/bin/env bash
# =============================================================================
# 本地起 app —— 直接跑 uvicorn（不经 Docker / K8s），供手工调 /chat 玩
# -----------------------------------------------------------------------------
# 做三件容易被忽略的事：
#   1. 把仓库根 .env 灌进环境 —— app 代码**不自己读 .env**（生产靠容器注入），
#      本地直跑必须显式 source，否则 LLM_MODE / LANGFUSE_* 全空。
#      ⚠️ 本机 .env 是 **CRLF** 存的（实测 51 行全带 CR），直接 source 会满屏
#      `./.env: line N: $'\r': command not found`，所以先 tr -d '\r' 再 source。
#   2. 清掉 shell 的 HTTP_PROXY/HTTPS_PROXY —— 本机代理会拦截 127.0.0.1 的请求，
#      表现为「/chat 连不上自己的 8000」。同时设 NO_PROXY 兜底。
#   3. cd 到 app/ 再起 —— main.py 用平级绝对导入（import llm / import observability），
#      在仓库根跑会 ModuleNotFoundError。
#
# ⚠️ 解释器不写死：按「托管 venv → D:\python314 → PATH 里的 python3/python」依次试，
#    每个候选都要能 import fastapi / uvicorn / prometheus_client 才算数。
#    启动横幅第一行会打印**实际用的那个**，排障先看它。
#
# ⚠️ app 用 langfuse SDK 上报 trace，SDK 默认 **5 秒** 定时 flush：
#    调完接口立刻 Ctrl+C 会丢最后一批 span。用 Ctrl+C 优雅退出（lifespan 会 flush），
#    或调用后多等 ~10 秒再看面板。
#
# 用法：
#   bash scripts/dev_app.sh              # 前台跑，Ctrl+C 退出
#   PORT=8010 bash scripts/dev_app.sh    # 换端口
# =============================================================================
set -uo pipefail

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# Python：按顺序找一个**能真正跑起来**的解释器
# -----------------------------------------------------------------------------
# ⚠️ 2026-10-30 修：原来只试托管 venv 一项，找不到就退回裸 `python`。
#    但 PowerShell / 某些 Git Bash 的 PATH 里并没有 `python`（Windows 上裸敲
#    `python` 常常只命中 Microsoft Store 的占位存根），于是最后一步变成
#    `exec: python: not found`，看起来像「环境坏了」。
#    现在按顺序试，并且**逐个验证能不能 import 依赖** —— 只判「文件存在」不够，
#    存在但缺包的下场一样：起不来，而且报错更靠后、更难懂。
PY_CANDIDATES=(
  "C:/Users/lile2/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
  "/c/Users/lile2/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
  "D:/python314/python.exe"
  "/d/python314/python.exe"
)
PY_NEEDS='import fastapi, uvicorn, prometheus_client'

pick_python() {
  local c
  # 显式指定优先：PY_OVERRIDE=... bash scripts/dev_app.sh
  if [ -n "${PY_OVERRIDE:-}" ]; then echo "$PY_OVERRIDE"; return 0; fi
  for c in "${PY_CANDIDATES[@]}"; do
    [ -x "$c" ] || continue
    if "$c" -c "$PY_NEEDS" >/dev/null 2>&1; then echo "$c"; return 0; fi
    echo "⚠️ 跳过 $c（文件在，但缺 fastapi / uvicorn / prometheus_client）" >&2
  done
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "$PY_NEEDS" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "❌ 找不到可用的 Python（要求能 import fastapi / uvicorn / prometheus_client）"
  echo "   试过这些："
  for c in "${PY_CANDIDATES[@]}"; do echo "     - $c"; done
  echo "     - PATH 里的 python3 / python"
  echo "   两种解法："
  echo "     ① 指定解释器：PY_OVERRIDE=D:/python314/python.exe bash scripts/dev_app.sh"
  echo "     ② 补依赖：    <解释器> -m pip install fastapi uvicorn prometheus_client"
  exit 1
fi

if [ ! -f .env ]; then
  echo "❌ 缺少 $REPO_ROOT/.env —— 先从 .env.example 复制并填好 LLM_* / LANGFUSE_*"
  exit 1
fi

# 1) 灌 .env（set -a 让后续赋的值自动 export）
# ⚠️ 2026-10-30 修：先剥掉 CRLF 再 source。
#    Windows 编辑器（记事本 / 某些 VS Code 设置）会把 .env 存成 CRLF，而 bash 会把
#    **空行**当成一条名叫 `\r` 的命令去执行，满屏报：
#      ./.env: line 2: $'\r': command not found
#    更阴的是值本身也带上尾随 \r：LLM_MODE 变成 "openai\r"，后面比较全不相等，
#    报错却出现在别处。所以这里一律 tr 一遍，比「要求人别用记事本」可靠。
ENV_TMP="/tmp/llmops_env.$$"
trap 'rm -f "$ENV_TMP"' EXIT
tr -d '\r' < ./.env > "$ENV_TMP"
set -a
# shellcheck disable=SC1090
. "$ENV_TMP"
set +a
rm -f "$ENV_TMP"

echo "──────────────────────────────────────────────"
echo " 解释器              = $PY"
echo " LLM_MODE            = ${LLM_MODE:-<未设>}"
echo " LLM_PRIMARY_MODEL   = ${LLM_PRIMARY_MODEL:-<未设>}"
echo " LLM_PRIMARY_BASE_URL= ${LLM_PRIMARY_BASE_URL:-<未设>}"
echo " OBSERVABILITY       = ${OBSERVABILITY:-<未设>}"
echo " LANGFUSE_HOST       = ${LANGFUSE_HOST:-<未设>}"
echo " 监听                = http://${HOST}:${PORT}"
echo " 交互文档            = http://${HOST}:${PORT}/docs"
echo "──────────────────────────────────────────────"

# 2) 清代理 + 3) 进 app/ 起服务
# ⚠️ 2026-10-23：原来是 `exec env -u HTTP_PROXY ... "$PY" -m uvicorn ...`，
#    在部分交互终端（Git Bash/mintty）里会打印完 banner 后**静默退出**（无输出、无进程、
#    退出码非 0），而同一台机器上直接 `"$PY" -m uvicorn ...` 正常。
#    改为先 unset 再 exec，少一层进程，行为与直接调用一致（已实测可起）。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
cd "$REPO_ROOT/app"
exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT"
