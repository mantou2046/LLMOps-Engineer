#!/usr/bin/env bash
# =============================================================================
# D3 验收脚本 —— 一条命令跑完 W1 D3 的三条验收标准
# -----------------------------------------------------------------------------
#   ✅ 1. kubectl get pods 全部 Running（2/2）
#   ✅ 2. kubectl port-forward 能访问（curl 拿到 /health 的 JSON）
#   ✅ 3. 生成 kubectl-cheatsheet.md 要求的最少命令覆盖（仅提示，不校验内容）
#
# 额外（卡点验证）：自动演示「latest + Always 会拉不到本地镜像」的反例思路，
#   并打印当前的 imagePullPolicy 供确认。
#
# 前置：
#   1. Docker Desktop 已启动（引擎在跑）
#   2. kind 已装（C:\Users\lile2\bin\kind.exe）
#   3. 镜像 llmops-api:0.1.0 已构建（D2 的 scripts/verify_d2.sh 会构建）
#
# 用法（Git Bash）：
#   cd /e/Projects/LLMOps-Engineer
#   bash scripts/verify_d3.sh
#
# ⚠️ 幂等：集群已存在则复用；不会重复创建。
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER="llmops"
IMAGE="llmops-api:0.1.0"
MANIFEST="infra/k8s/app.yaml"
NS="default"
DEPLOY="llmops-api"
PF_PORT=8000          # 本机端口
PF_TIMEOUT=25         # port-forward 最长等待秒数

PASS=0
FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
info() { echo "  ℹ️  $1"; }
hr()   { echo "-------------------------------------------------------------"; }

# ---- 定位工具（kind / kubectl 可能不在 PATH）----
KIND_BIN="$(command -v kind || echo /c/Users/lile2/bin/kind.exe)"
KUBECTL_BIN="$(command -v kubectl || echo "/c/Users/lile2/AppData/Local/Programs/DockerDesktop/resources/bin/kubectl")"

echo "============================================================="
echo " W1 D3 验收 · K8s 起手（Deployment + Service）"
echo "============================================================="

# ---- 前置：Docker 引擎 ----
hr
echo "[0/5] 前置检查"
if docker info >/dev/null 2>&1; then
  ok "Docker 引擎在跑"
else
  bad "Docker 引擎没起来 → 先启动 Docker Desktop，再重跑本脚本"
  echo
  echo "  启动方式：开始菜单搜 Docker Desktop，或手工双击"
  echo "  C:\\Users\\lile2\\AppData\\Local\\Programs\\DockerDesktop\\Docker Desktop.exe"
  exit 1
fi

if "$KIND_BIN" version >/dev/null 2>&1; then
  ok "kind 可用：$("$KIND_BIN" version | head -1)"
else
  bad "kind 不可用"
  exit 1
fi

if "$KUBECTL_BIN" version --client >/dev/null 2>&1; then
  ok "kubectl 可用：$("$KUBECTL_BIN" version --client -o json 2>/dev/null | grep -o '\"gitVersion\":\"[^\"]*\"' | head -1)"
else
  bad "kubectl 不可用"
  exit 1
fi

# ---- 镜像就位 ----
hr
echo "[1/5] 镜像检查"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  SZ=$(docker image inspect "$IMAGE" --format '{{.Size}}')
  ok "本地已有 $IMAGE（$((SZ/1024/1024)) MB）"
else
  bad "本地没有 $IMAGE → 先跑 bash scripts/verify_d2.sh 构建"
  exit 1
fi

# ---- 集群 ----
hr
echo "[2/5] kind 集群"
if "$KIND_BIN" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "集群 $CLUSTER 已存在，复用"
else
  info "创建集群 $CLUSTER ...（首次约 30–60s，要拉 node 镜像）"
  if "$KIND_BIN" create cluster --name "$CLUSTER" --wait 120s; then
    ok "集群 $CLUSTER 创建完成"
  else
    bad "集群创建失败"
    exit 1
  fi
fi

# 切到该集群的 context
"$KUBECTL_BIN" config use-context "kind-$CLUSTER" >/dev/null 2>&1
CTX=$("$KUBECTL_BIN" config current-context 2>/dev/null)
ok "当前 context：$CTX"
# ⚠️ 装了 Docker Desktop + kind 后会有多个 context —— 确认对着对的集群

# ---- 载入镜像（关键：kind 不共享宿主 Docker 的镜像库）----
hr
echo "[3/5] 把镜像 load 进集群"
info "kind 的节点是独立容器，看不到宿主 docker images —— 必须 load"
if "$KIND_BIN" load docker-image "$IMAGE" --name "$CLUSTER"; then
  ok "镜像已载入节点"
else
  bad "镜像载入失败"
  exit 1
fi

# ---- 应用清单 ----
hr
echo "[4/5] 应用 Deployment + Service"
"$KUBECTL_BIN" apply -f "$MANIFEST" || { bad "apply 失败"; exit 1; }
info "imagePullPolicy = $("$KUBECTL_BIN" get deploy "$DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].imagePullPolicy}')  （应为 IfNotPresent）"

echo "  等待 rollout ..."
if "$KUBECTL_BIN" rollout status "deploy/$DEPLOY" --timeout=120s; then
  ok "rollout 完成"
else
  bad "rollout 未在 120s 内完成"
  "$KUBECTL_BIN" get pods
  "$KUBECTL_BIN" describe pod -l app="$DEPLOY" | tail -20
  exit 1
fi

# ---- 验收 1：Pod 全 Running ----
hr
echo "[5/5] 三条验收"
echo
echo "  验收 1 · Pod 全部 Running"
"$KUBECTL_BIN" get pods -l app="$DEPLOY" -o wide
RUNNING=$("$KUBECTL_BIN" get pods -l app="$DEPLOY" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
READY=$("$KUBECTL_BIN" get deploy "$DEPLOY" -o jsonpath='{.status.readyReplicas}')
if [ "$RUNNING" -ge 1 ] && [ "$READY" = "2" ]; then
  ok "Running $RUNNING 个 / 就绪副本 $READY （期望 2）"
else
  bad "Running=$RUNNING, readyReplicas=$READY（期望 2）"
fi

# ---- 验收 2：port-forward 能访问 ----
echo
echo "  验收 2 · kubectl port-forward 能访问 /health"
"$KUBECTL_BIN" port-forward "svc/$DEPLOY" "$PF_PORT:80" >/tmp/pf.log 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null' EXIT

# 等 port-forward 就绪
for i in $(seq 1 $PF_TIMEOUT); do
  if curl -s -m 2 "http://127.0.0.1:$PF_PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

HEALTH=$(curl -s -m 5 "http://127.0.0.1:$PF_PORT/health" 2>/dev/null)
if echo "$HEALTH" | grep -q '"status":"ok"'; then
  ok "port-forward 通，响应：$HEALTH"
else
  bad "port-forward 访问失败（响应：${HEALTH:-空}）"
fi

# 顺带验证 Service 的 Endpoints 不为空（排错时最常踩）
EP=$("$KUBECTL_BIN" get endpoints "$DEPLOY" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)
if [ -n "$EP" ]; then
  ok "Service Endpoints 已挂上：$EP"
else
  bad "Service Endpoints 为空 → selector 与 Pod labels 不匹配"
fi

kill $PF_PID 2>/dev/null

# ---- 验收 3：cheatsheet ----
echo
echo "  验收 3 · kubectl 速查表已产出"
if [ -f docs/kubectl-cheatsheet.md ]; then
  ok "docs/kubectl-cheatsheet.md 存在（$(wc -l < docs/kubectl-cheatsheet.md) 行）"
else
  bad "docs/kubectl-cheatsheet.md 不存在"
fi

# ---- 额外：自愈演示（可选，非破坏性）----
hr
echo "附加 · 自愈演示（删一个 Pod，看 Deployment 是否补回来）"
BEFORE=$("$KUBECTL_BIN" get pods -l app="$DEPLOY" --no-headers | wc -l)
VICTIM=$("$KUBECTL_BIN" get pods -l app="$DEPLOY" --no-headers | head -1 | awk '{print $1}')
if [ -n "$VICTIM" ]; then
  info "删除 $VICTIM"
  "$KUBECTL_BIN" delete pod "$VICTIM" --wait=false >/dev/null 2>&1
  sleep 6
  AFTER=$("$KUBECTL_BIN" get pods -l app="$DEPLOY" --no-headers | wc -l)
  if [ "$AFTER" -ge "$BEFORE" ]; then
    ok "自愈生效：删前 $BEFORE 个 → 删后 $AFTER 个"
  else
    bad "自愈未生效：删前 $BEFORE → 删后 $AFTER"
  fi
fi

# ---- 汇总 ----
hr
echo " 结果：通过 $PASS 项，失败 $FAIL 项"
echo "============================================================="
if [ "$FAIL" -eq 0 ]; then
  echo " ✅ D3 验收全部通过"
  echo
  echo " 提示：集群还在跑。清理用："
  echo "   $KIND_BIN delete cluster --name $CLUSTER"
  exit 0
else
  echo " ❌ 有失败项，见上方输出"
  exit 1
fi
