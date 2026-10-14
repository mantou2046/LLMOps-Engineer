#!/usr/bin/env bash
# =============================================================================
# 本地起 app —— 直接跑 uvicorn（不经 Docker / K8s），供手工调 /chat 玩
# -----------------------------------------------------------------------------
# 做三件容易被忽略的事：
#   1. 把仓库根 .env 灌进环境 —— app 代码**不自己读 .env**（生产靠容器注入），
#      本地直跑必须显式 source，否则 LLM_MODE / LANGFUSE_* 全空。
#   2. 清掉 shell 的 HTTP_PROXY/HTTPS_PROXY —— 本机代理会拦截 127.0.0.1 的请求，
#      表现为「/chat 连不上自己的 8000」。同时设 NO_PROXY 兜底。
#   3. cd 到 app/ 再起 —— main.py 用平级绝对导入（import llm / import observability），
#      在仓库根跑会 ModuleNotFoundError。
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

# Python：优先用托管 venv（依赖齐全），找不到再退回系统解释器
PY="${PY:-C:/Users/lile2/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe}"
if [ ! -x "$PY" ]; then
  echo "⚠️ 未找到 $PY，退回 python"
  PY="python"
fi

if [ ! -f .env ]; then
  echo "❌ 缺少 $REPO_ROOT/.env —— 先从 .env.example 复制并填好 LLM_* / LANGFUSE_*"
  exit 1
fi

# 1) 灌 .env（set -a 让后续赋的值自动 export）
set -a
# shellcheck disable=SC1091
. ./.env
set +a

echo "──────────────────────────────────────────────"
echo " LLM_MODE            = ${LLM_MODE:-<未设>}"
echo " LLM_PRIMARY_MODEL   = ${LLM_PRIMARY_MODEL:-<未设>}"
echo " LLM_PRIMARY_BASE_URL= ${LLM_PRIMARY_BASE_URL:-<未设>}"
echo " OBSERVABILITY       = ${OBSERVABILITY:-<未设>}"
echo " LANGFUSE_HOST       = ${LANGFUSE_HOST:-<未设>}"
echo " 监听                = http://${HOST}:${PORT}"
echo " 交互文档            = http://${HOST}:${PORT}/docs"
echo "──────────────────────────────────────────────"

# 2) 清代理 + 3) 进 app/ 起服务
cd "$REPO_ROOT/app"
exec env \
  -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT"
