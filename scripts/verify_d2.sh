#!/usr/bin/env bash
# =============================================================================
# D2 验收脚本 —— 一条命令跑完 W1 D2 的三条验收标准
# -----------------------------------------------------------------------------
#   ✅ 1. 多阶段镜像体积 < 200MB，且显著小于朴素单阶段镜像（并打印对比）
#   ✅ 2. 容器内 whoami 不是 root
#   ✅ 3. pgvector 可连，并能完成一次向量写入与相似度检索
#
# 前置：Docker Desktop 已运行；仓库根已有 .env（含 POSTGRES_PASSWORD）
#
# 用法（Git Bash / WSL / Linux）：
#     cd /e/Projects/LLMOps-Engineer
#     bash scripts/verify_d2.sh
#
# ⚠️ 同时会构建两个镜像做体积对比：llmops-api:0.1.0（多阶段）与
#    llmops-api:naive（朴素单阶段，用 Dockerfile.naive）。后者仅用于对比。
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE_MULTI="llmops-api:0.1.0"
IMAGE_NAIVE="llmops-api:naive"
SIZE_LIMIT_MB=200

PASS=0
FAIL=0
note() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✅ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[1;31m  ❌ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
info() { printf '     %s\n' "$*"; }

# ---- 前置检查 ---------------------------------------------------------------
note "前置检查"
if ! command -v docker >/dev/null 2>&1; then
  bad "找不到 docker 命令。新开一个终端让 PATH 生效；或见 infra/DOCKER-SETUP-Win11.md"
  exit 1
fi
ok "docker: $(docker --version)"
docker compose version >/dev/null 2>&1 && ok "compose: $(docker compose version --short)" || bad "docker compose 不可用"

if [ ! -f .env ]; then
  bad "缺少 .env —— 先执行： cp .env.example .env 并填 POSTGRES_PASSWORD"
  exit 1
fi
ok ".env 存在"
# shellcheck disable=SC1091
set -a; . ./.env; set +a
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  bad ".env 里 POSTGRES_PASSWORD 为空，请填写"
  exit 1
fi
ok "POSTGRES_PASSWORD 已设置（值不打印）"

# =============================================================================
note "验收 1/3 · 镜像体积（多阶段 vs 朴素，目标 < ${SIZE_LIMIT_MB}MB）"
# =============================================================================
info "构建朴素单阶段镜像（仅用于对比，多花 1–2 分钟）..."
if docker build -q -f app/Dockerfile.naive -t "$IMAGE_NAIVE" . >/dev/null 2>&1; then
  ok "naive 镜像构建成功"
else
  info "⚠️ naive 镜像构建失败（不影响主验收），跳过体积对比"
  IMAGE_NAIVE=""
fi

info "构建多阶段镜像..."
if docker build -f app/Dockerfile -t "$IMAGE_MULTI" . >/tmp/d2_build.log 2>&1; then
  ok "多阶段镜像构建成功"
  # 打印「构建上下文体积」—— .dockerignore 的效果证据
  if grep -qi "transferring context" /tmp/d2_build.log; then
    info "构建上下文：$(grep -i 'transferring context' /tmp/d2_build.log | head -1 | tr -s ' ')"
  fi
else
  bad "多阶段镜像构建失败，日志尾部："
  tail -20 /tmp/d2_build.log | sed 's/^/       /'
fi

bytes_of() { docker image inspect "$1" --format '{{.Size}}' 2>/dev/null || echo 0; }
human() { awk -v b="$1" 'BEGIN{printf "%.1f MB", b/1024/1024}'; }

if docker image inspect "$IMAGE_MULTI" >/dev/null 2>&1; then
  S_MULTI=$(bytes_of "$IMAGE_MULTI"); MB_MULTI=$(awk -v b="$S_MULTI" 'BEGIN{printf "%.0f", b/1024/1024}')
  if [ -n "$IMAGE_NAIVE" ] && docker image inspect "$IMAGE_NAIVE" >/dev/null 2>&1; then
    S_NAIVE=$(bytes_of "$IMAGE_NAIVE"); MB_NAIVE=$(awk -v b="$S_NAIVE" 'BEGIN{printf "%.0f", b/1024/1024}')
    printf '\n     %-22s %10s\n' "镜像" "体积"
    printf '     %-22s %10s\n' "------------------------" "----------"
    printf '     %-22s %10s\n' "朴素单阶段 (naive)" "$(human "$S_NAIVE")"
    printf '     %-22s %10s\n' "多阶段 (multi-stage)" "$(human "$S_MULTI")"
    if [ "$S_NAIVE" -gt 0 ]; then
      SAVED=$(awk -v a="$S_NAIVE" -v b="$S_MULTI" 'BEGIN{printf "%.1f", (a-b)*100/a}')
      printf '     %-22s %10s\n' "减少" "${SAVED}%"
    fi
    [ "$MB_MULTI" -lt "$SIZE_LIMIT_MB" ] && ok "多阶段镜像 ${MB_MULTI}MB < ${SIZE_LIMIT_MB}MB" \
                                        || bad "多阶段镜像 ${MB_MULTI}MB 未达 < ${SIZE_LIMIT_MB}MB"
    [ "$S_MULTI" -lt "$S_NAIVE" ] && ok "确实小于朴素镜像" || bad "未小于朴素镜像（.dockerignore / 分层没生效？）"
  else
    [ "$MB_MULTI" -lt "$SIZE_LIMIT_MB" ] && ok "多阶段镜像 ${MB_MULTI}MB < ${SIZE_LIMIT_MB}MB" \
                                        || bad "多阶段镜像 ${MB_MULTI}MB 未达 < ${SIZE_LIMIT_MB}MB"
  fi
fi

# =============================================================================
note "验收 2/3 · 容器内非 root"
# =============================================================================
if docker run --rm "$IMAGE_MULTI" whoami 2>/dev/null | grep -q .; then
  WHOAMI=$(docker run --rm "$IMAGE_MULTI" whoami 2>/dev/null)
  UID_LINE=$(docker run --rm "$IMAGE_MULTI" id 2>/dev/null)
  info "whoami -> $WHOAMI"
  info "id     -> $UID_LINE"
  if [ "$WHOAMI" != "root" ]; then ok "容器内以非 root 用户运行（$WHOAMI）"
  else bad "容器内是 root，USER 指令没生效"; fi
else
  bad "容器起不来，无法检查用户（看上面的构建日志）"
fi

# =============================================================================
note "验收 3/3 · pgvector 可连 + 向量读写"
# =============================================================================
info "启动 Postgres + pgvector..."
docker compose -f infra/compose.yml up -d >/dev/null 2>&1 || bad "compose up 失败"

info "等待 healthy（最多 90s）..."
HEALTHY=0
for i in $(seq 1 30); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' llmops-postgres 2>/dev/null || echo "missing")
  if [ "$ST" = "healthy" ]; then HEALTHY=1; break; fi
  sleep 3
done
if [ "$HEALTHY" = "1" ]; then
  ok "postgres 状态 healthy"
else
  bad "postgres 未在 90s 内 healthy（当前：${ST:-unknown}）"
  docker compose -f infra/compose.yml logs --tail 30 postgres | sed 's/^/       /'
fi

# 优先用容器内的 psql 跑一遍 SQL（不依赖宿主 python 装 psycopg）
if [ "$HEALTHY" = "1" ]; then
  info "容器内 psql 自检（扩展 + 向量读写）..."
  # ⚠️ SET client_min_messages 抑制 CREATE/DROP IF EXISTS 产生的 NOTICE ——
  #    否则 NOTICE 会混进 stdout 把结果解析带偏（2026-10-08 实际踩过：断言把 NOTICE 当成了查询结果）
  PSQL_OUT=$(docker exec llmops-postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v ON_ERROR_STOP=1 -tA -c "
    SET client_min_messages TO WARNING;
    CREATE EXTENSION IF NOT EXISTS vector;
    DROP TABLE IF EXISTS d2_check;
    CREATE TABLE d2_check (id serial primary key, label text, embedding vector(3));
    INSERT INTO d2_check (label, embedding) VALUES
      ('条款A', '[1,0,0]'), ('条款B', '[0.98,0.19,0]'), ('条款C', '[0,1,0]');
    SELECT label || '|' || round((1 - (embedding <=> '[1,0.05,0]'::vector))::numeric, 4)
      FROM d2_check ORDER BY embedding <=> '[1,0.05,0]'::vector LIMIT 2;
  " 2>&1)
  RC=$?
  if [ $RC -eq 0 ]; then
    echo "$PSQL_OUT" | sed 's/^/       /'
    # 只取形如「条款X|0.xxxx」的结果行，防任何日志/警告污染解析
    TOP1=$(echo "$PSQL_OUT" | grep -E '^[^|]+\|[0-9]+\.[0-9]+$' | head -1 | cut -d'|' -f1)
    if [ "$TOP1" = "条款A" ]; then
      ok "向量写入与最近邻检索正确（Top-1 = 条款A）"
    else
      bad "检索结果不符预期（Top-1 = $TOP1，期望 条款A）"
    fi
  else
    bad "psql 自检失败："
    echo "$PSQL_OUT" | tail -10 | sed 's/^/       /'
  fi

  # 若宿主装了 psycopg，再跑一次更完整的脚本
  if python -c "import psycopg" 2>/dev/null; then
    info "宿主已装 psycopg，跑完整自检脚本..."
    python infra/check_pgvector.py 2>&1 | sed 's/^/       /' && ok "check_pgvector.py 通过" || bad "check_pgvector.py 未通过"
  else
    info "（宿主未装 psycopg，跳过 check_pgvector.py；容器内 psql 已验证通路）"
  fi
fi

# =============================================================================
note "结果汇总"
printf '     通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32m     D2 验收全部通过 ✅  可以打卡了\033[0m\n'
  printf '     善后： docker compose -f infra/compose.yml down    # 保留数据\n'
  exit 0
else
  printf '\033[1;31m     D2 验收存在失败项 ❌  见上方明细\033[0m\n'
  exit 1
fi
