#!/usr/bin/env bash
# D16 重启验证脚本 —— 确认「服务重启后，新请求带上了 prompt 版本」
#
# 用途：验证两件事
#   ① 响应头里有 X-Prompt-Version / X-Prompt-Hash（机器可读）
#   ② 请求发到了 Langfuse（trace 头部会有 prompt: structured_json · v1，需去 UI 看）
#
# 用法：
#   1) 另开一个窗口起服务（前台常驻）：
#        cd /e/Projects/LLMOps-Engineer/app
#        "$PY" -m uvicorn main:app --port 8000
#   2) 在任意窗口跑本脚本：
#        cd /e/Projects/LLMOps-Engineer && bash scripts/verify_d16_restart.sh
#
# 可选环境变量：HOST（默认 127.0.0.1）、PORT（默认 8000）、EXPECT（默认 structured_json@v1）

set -u

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
EXPECT="${EXPECT:-structured_json@v1}"
BASE="http://${HOST}:${PORT}"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=1; }
info() { printf '    %s\n' "$1"; }

FAIL=0
echo "== D16 重启验证 =="
echo "目标：${BASE}   期望版本：${EXPECT}"
echo

# ---------- 0. 服务在不在 ----------
CODE=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 "${BASE}/health" || echo 000)
if [ "$CODE" = "200" ]; then
  ok "服务在线（/health 200）"
else
  bad "服务不可达（/health 返回 ${CODE}）"
  info "先另开窗口起服务：cd /e/Projects/LLMOps-Engineer/app && \"\$PY\" -m uvicorn main:app --port 8000"
  exit 1
fi

# ---------- 1. 打一发 /chat，取响应头 ----------
# 取「状态行 + 响应头 + 响应体」全文（不落临时文件，避免 Windows safe-delete 噪音）
RESP=$(curl --noproxy '*' -s -i --max-time 180 \
  -X POST "${BASE}/chat" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"ping d16 restart check"}],"structured":true}' || true)
# ⚠️ payload 故意用纯 ASCII：Windows 控制台若为 GBK(chcp 936)，中文会被转成 GBK 字节，
#    服务端按 UTF-8 解码失败 → FastAPI 直接回 400，且不产生 chat span（现象：trace 里只有
#    `POST /chat` 一条、status_code=400、无 chat.primary generation）。2026-10-22 实踩。

STATUS=$(printf '%s' "$RESP" | head -1 | tr -d '\r')
VER=$(printf '%s' "$RESP"  | grep -i '^x-prompt-version:'     | tr -d '\r' | awk '{print $2}')
HASH=$(printf '%s' "$RESP" | grep -i '^x-prompt-hash:'        | tr -d '\r' | awk '{print $2}')
UP=$(printf '%s' "$RESP"   | grep -i '^x-upstream-attempts:'  | tr -d '\r' | awk '{print $2}')
BODY=$(printf '%s' "$RESP" | sed -n '/^\r*$/,$p' | tail -n +2 | head -c 300)

echo
if printf '%s' "$STATUS" | grep -q ' 200 '; then
  ok "POST /chat → ${STATUS}"
else
  bad "POST /chat → ${STATUS:-（无响应）}"
  [ -n "${BODY:-}" ] && info "响应体：${BODY}"
  info "502/500 时错误响应不带版本头——这是上游问题，不是代码问题；等几秒重跑本脚本"
  info "若状态是 404/405：服务可能是旧的，或端口上跑的不是这个应用"
  info "若是 400：多为请求体编码问题（GBK 控制台把中文弄坏）——本脚本已用纯 ASCII payload，仍 400 请查是否手工 curl 带了中文"
fi

echo
if [ -n "${VER:-}" ]; then
  ok "X-Prompt-Version = ${VER}"
else
  bad "没有 X-Prompt-Version 响应头"
  info "可能原因：① 服务还是旧代码（没重启）② 请求里没带 structured:true"
fi

if [ -n "${HASH:-}" ]; then
  ok "X-Prompt-Hash    = ${HASH}"
else
  bad "没有 X-Prompt-Hash 响应头"
fi

if [ -n "${UP:-}" ]; then info "上游尝试次数 = ${UP}"; fi

# ---------- 2. 版本是否符合期望 ----------
echo
if [ "${VER:-}" = "$EXPECT" ]; then
  ok "版本与期望一致（${EXPECT}）"
else
  bad "版本是 ${VER:-（空）}，期望 ${EXPECT}"
  info "若你刚拨过 label：换版有 TTL(60s)+1 个请求的延迟，等 60 秒再打一发"
fi

# ---------- 3. 下一步（UI 侧） ----------
echo
echo "== 接下来去 Langfuse 看（脚本看不到 UI）=="
echo "  1) Tracing → 最新那条 chat / chat.primary → 头部应显示：prompt: structured_json · v1"
echo "  2) Prompt Management → structured_json → #1 → Linked Generations 计数 +1（之前是 2）"
echo "  3) 若 Tracing 里没有这条：确认 .env 里 OBSERVABILITY=langfuse（2026-10-22 已修好「.env 读得太晚」的 bug）"
echo

if [ "$FAIL" -eq 0 ]; then
  printf '\033[32m== 脚本侧全过 ==\033[0m\n'
else
  printf '\033[31m== 有检查未通过（见上）==\033[0m\n'
fi
exit "$FAIL"
