#!/usr/bin/env bash
# =============================================================================
# D4 验收脚本 —— W1 D4「K8s 配置与暴露」
# -----------------------------------------------------------------------------
#   ✅ 1. 配置外置 —— ConfigMap + Secret 存在，且容器里能读到
#   ✅ 2. 数据持久化 —— 删掉 Pod 重建后数据还在（本日核心）
#   ✅ 3. Ingress 能访问到服务
#   ✅ 附加：Secret 是 base64 不是加密（动手演示）
#
# 前置：D3 的集群在跑（kind-llmops）；pgvector/pgvector:pg16 镜像本地已有
#
# 用法：
#   cd /e/Projects/LLMOps-Engineer
#   bash scripts/verify_d4.sh
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER="llmops"
NS="default"
KIND_BIN="$(command -v kind || echo /c/Users/lile2/bin/kind.exe)"
KUBECTL_BIN="$(command -v kubectl || echo "/c/Users/lile2/AppData/Local/Programs/DockerDesktop/resources/bin/kubectl")"

PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
info() { echo "  ℹ️  $1"; }
hr()   { echo "-------------------------------------------------------------"; }

echo "============================================================="
echo " W1 D4 验收 · 配置外置 + 持久化 + Ingress"
echo "============================================================="

# ---- 前置 ----
hr
echo "[0/6] 前置检查"
if ! docker info >/dev/null 2>&1; then bad "Docker 引擎没跑"; exit 1; fi
ok "Docker 引擎在跑"
if ! "$KIND_BIN" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  bad "集群 $CLUSTER 不存在 → 先跑 bash scripts/verify_d3.sh"; exit 1
fi
ok "集群 $CLUSTER 存在"
"$KUBECTL_BIN" config use-context "kind-$CLUSTER" >/dev/null 2>&1
ok "context：$("$KUBECTL_BIN" config current-context)"
docker image inspect pgvector/pgvector:pg16 >/dev/null 2>&1 && ok "pgvector 镜像本地已有" || { bad "缺 pgvector/pgvector:pg16"; exit 1; }

# ---- 1. 配置外置 ----
hr
echo "[1/6] 配置外置（ConfigMap + Secret）"
"$KUBECTL_BIN" apply -f infra/k8s/config.yaml >/dev/null || { bad "config.yaml apply 失败"; exit 1; }
"$KUBECTL_BIN" get configmap llmops-config >/dev/null 2>&1 && ok "ConfigMap llmops-config 已创建" || bad "ConfigMap 缺失"
"$KUBECTL_BIN" get secret llmops-secrets >/dev/null 2>&1 && ok "Secret llmops-secrets 已创建" || bad "Secret 缺失"

# 演示：Secret 只是 base64，不是加密
echo
info "演示 —— Secret 只是 base64 而非加密："
if "$KUBECTL_BIN" get secret llmops-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' >/tmp/sec.b64 2>/dev/null; then
  DECODED=$(cat /tmp/sec.b64 | base64 -d 2>/dev/null)
  info "  kubectl get secret -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d"
  info "  → 直接还原出明文：${DECODED}"
  ok "已证实：能读到 Secret 的人 = 能拿到明文（⚠️ 所以绝不能提交进 Git）"
fi

# ---- 2. 持久化 ----
hr
echo "[2/6] 部署 pgvector（StatefulSet + PVC）"
"$KIND_BIN" load docker-image pgvector/pgvector:pg16 --name "$CLUSTER" >/dev/null 2>&1 && ok "pgvector 镜像已载入节点"
"$KUBECTL_BIN" apply -f infra/k8s/postgres.yaml >/dev/null || { bad "postgres.yaml apply 失败"; exit 1; }
info "等待 postgres-0 就绪 ..."
if "$KUBECTL_BIN" rollout status statefulset/postgres --timeout=180s >/dev/null 2>&1; then
  ok "postgres-0 已就绪"
else
  bad "postgres 未在 180s 内就绪"
  "$KUBECTL_BIN" describe pod postgres-0 | tail -20
  exit 1
fi

# ---- 3. PVC 绑定 ----
hr
echo "[3/6] PVC / StorageClass"
"$KUBECTL_BIN" get pvc
PVC_NAME=$("$KUBECTL_BIN" get pvc -o name 2>/dev/null | grep data-postgres-0 | head -1)
if [ -n "$PVC_NAME" ]; then
  PHASE=$("$KUBECTL_BIN" get "$PVC_NAME" -o jsonpath='{.status.phase}')
  SC=$("$KUBECTL_BIN" get "$PVC_NAME" -o jsonpath='{.spec.storageClassName}')
  if [ "$PHASE" = "Bound" ]; then
    ok "PVC $(basename $PVC_NAME) 已 Bound（StorageClass=$SC）"
  else
    bad "PVC 状态为 $PHASE（期望 Bound）"
  fi
else
  bad "没找到 data-postgres-0 这个 PVC"
fi

# ---- 4. 持久化核心验收：写 → 删 Pod → 重建 → 读 ----
hr
echo "[4/6] ⭐ 持久化核心验收（写 → 删 Pod → 重建 → 数据还在？）"

PG_EXEC() { "$KUBECTL_BIN" exec statefulset/postgres -- psql -U llmops -d llmops -tAc "$1" 2>/dev/null; }

# 写入
MARK="d4-persist-$(date +%s)"
PG_EXEC "INSERT INTO persistence_demo(note) VALUES ('$MARK');" >/dev/null 2>&1
BEFORE=$(PG_EXEC "SELECT count(*) FROM persistence_demo WHERE note='$MARK';")
if [ "$BEFORE" = "1" ]; then
  ok "写入成功：note='$MARK'（count=$BEFORE）"
else
  bad "写入失败（count=$BEFORE）"
fi

# 删掉整个 Pod（StatefulSet 会重建同名的，且挂回同一个 PVC）
info "删除 pod/postgres-0（模拟节点故障/重建）..."
"$KUBECTL_BIN" delete pod postgres-0 --wait=true >/dev/null 2>&1
info "等待重建 ..."
"$KUBECTL_BIN" rollout status statefulset/postgres --timeout=180s >/dev/null 2>&1

# 读回
AFTER=$(PG_EXEC "SELECT count(*) FROM persistence_demo WHERE note='$MARK';")
if [ "$AFTER" = "1" ]; then
  ok "⭐ 删 Pod 重建后数据仍在（count=$AFTER）→ 持久化成立"
else
  bad "数据丢失！（count=$AFTER）"
fi

# 顺带：确认 pgvector 扩展可用
VEC=$(PG_EXEC "SELECT extname FROM pg_extension WHERE extname='vector';")
[ "$VEC" = "vector" ] && ok "pgvector 扩展已启用" || info "pgvector 扩展未启用（initdb 只在空数据目录时跑）"

# ---- 5. Ingress ----
hr
echo "[5/6] Ingress 暴露"
if "$KUBECTL_BIN" get ingressclass nginx >/dev/null 2>&1; then
  ok "ingress-nginx 控制器已装"
  "$KUBECTL_BIN" apply -f infra/k8s/ingress.yaml >/dev/null 2>&1
  sleep 5
  info "Ingress 已应用（host: llmops.local）"
  # 通过宿主 80 端口访问（依赖 kind extraPortMappings → 节点 30080）
  CODE=$(curl -s -o /tmp/ing.out -w '%{http_code}' -m 8 -H "Host: llmops.local" http://localhost/health 2>/dev/null)
  if [ "$CODE" = "200" ] && grep -q '"status":"ok"' /tmp/ing.out 2>/dev/null; then
    ok "Ingress 访问成功：http://llmops.local/health → $(cat /tmp/ing.out)"
  else
    bad "Ingress 访问失败（HTTP $CODE）"
    info "  排查：kubectl get pods -n ingress-nginx / describe ingress llmops-api"
  fi
  # ⭐ 负向验证：错误 Host 必须 404 —— 这才证明是 Ingress 在做 Host 路由，
  #    而不是碰巧撞到了 Service（没有这一步，200 也可能来自别处）
  BADCODE=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "Host: wrong.local" http://localhost/health 2>/dev/null)
  if [ "$BADCODE" = "404" ]; then
    ok "负向验证：错误 Host → 404（确认是 Ingress 按 Host 路由）"
  else
    bad "错误 Host 返回 $BADCODE（期望 404，说明路由规则可能没生效）"
  fi
else
  info "⏭️  ingress-nginx 尚未安装 → 跳过（先跑 scripts/setup_ingress_kind.sh）"
fi

# ---- 6. 清单齐全 ----
hr
echo "[6/6] 产出物清单"
for f in infra/k8s/config.yaml infra/k8s/postgres.yaml infra/k8s/ingress.yaml infra/k8s/kind-ingress-config.yaml; do
  [ -f "$f" ] && ok "$f" || bad "$f 缺失"
done

# ---- 汇总 ----
hr
echo " 结果：通过 $PASS 项，失败 $FAIL 项"
echo "============================================================="
[ "$FAIL" -eq 0 ] && { echo " ✅ D4 验收全部通过"; exit 0; } || { echo " ❌ 有失败项"; exit 1; }
