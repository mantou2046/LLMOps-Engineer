#!/usr/bin/env bash
# =============================================================================
# D5 验收脚本 —— W1 D5「探针与排错」
# -----------------------------------------------------------------------------
#   ✅ 1. 三种探针都在清单里且参数正确（配置层）
#   ✅ 2. startupProbe 真实生效：启动期 liveness 不执行（核心）
#   ✅ 3. readinessProbe 真实生效：失败 → 从 EndpointSlice 摘掉，**不重启**
#   ✅ 4. 三种排错路径各演练一次（错 tag / OOM / 路径错）
#
# 设计原则（沿用 verify_d3/d4）：
#   · 每一项都必须能「证伪」—— 断言失败要能说清是哪儿不对
#   · 关键项做**对照**：正确配置 vs 错误配置，用差异证明机制
#
# ⚠️ 本脚本会**临时改动集群状态**（注入探针失败、创建演练 Deployment），
#    结束时自动还原。中途异常退出时，手动恢复：
#      kubectl apply -f infra/k8s/app.yaml
#      kubectl delete deployment oom-demo broken-tag bad-ready --ignore-not-found
#
# 前置：D4 的集群在跑（kind-llmops），llmops-api:0.2.0 已 kind load
#
# 用法：
#   cd /e/Projects/LLMOps-Engineer
#   bash scripts/verify_d5.sh
# =============================================================================
set -uo pipefail

# ⚠️ Windows 下必须关掉 MSYS 路径转换：否则传给 kubectl/docker 的
#    /tmp/... 会被改写成 C:/Users/.../Temp/...，导致「文件不存在」。
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER="llmops"
KIND_BIN="$(command -v kind || echo /c/Users/lile2/bin/kind.exe)"
KUBECTL_BIN="$(command -v kubectl || echo "/c/Users/lile2/AppData/Local/Programs/DockerDesktop/resources/bin/kubectl")"
K() { "$KUBECTL_BIN" "$@"; }

PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
info() { echo "  ℹ️  $1"; }
hr()   { echo "-------------------------------------------------------------"; }

echo "============================================================="
echo " W1 D5 验收 · 三种探针 + 排错演练"
echo "============================================================="

# ---- 前置 ----
hr
echo "[0/5] 前置检查"
if ! docker info >/dev/null 2>&1; then bad "Docker 引擎没跑"; exit 1; fi
ok "Docker 引擎在跑"
if ! "$KIND_BIN" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  bad "集群 $CLUSTER 不存在 → 先跑 bash scripts/verify_d3.sh"; exit 1
fi
ok "集群 $CLUSTER 存在"
K config use-context "kind-$CLUSTER" >/dev/null 2>&1
ok "context：$(K config current-context)"
# 镜像必须在节点里（kind 节点看不到宿主镜像）
if docker exec "$CLUSTER-control-plane" crictl images 2>/dev/null | grep -q 'llmops-api.*0.2.0'; then
  ok "镜像 llmops-api:0.2.0 已在节点内"
else
  bad "节点内缺 llmops-api:0.2.0 → 跑 kind load docker-image llmops-api:0.2.0 --name $CLUSTER"
  exit 1
fi

# 部署应用（幂等）
K apply -f infra/k8s/app.yaml >/dev/null 2>&1 || { bad "app.yaml apply 失败"; exit 1; }
if K rollout status deployment/llmops-api --timeout=120s >/dev/null 2>&1; then
  ok "llmops-api 已就绪（2 副本）"
else
  bad "llmops-api 未在 120s 内就绪"
  K get pods -l app=llmops-api
  exit 1
fi

# ---- 1. 三种探针配置正确 ----
hr
echo "[1/5] 三种探针的配置（读清单实际值，不信注释）"

# 用 kubectl 自带的 jsonpath 取字段 —— 不依赖外部 python，跨机器可跑
probe_field() { # $1=探针名 $2=字段路径（如 httpGet.path）
  K get deployment llmops-api -o jsonpath="{.spec.template.spec.containers[0].$1.$2}" 2>/dev/null
}

check_probe() { # $1=探针名 $2=期望路径 $3=期望period $4=期望failure
  local got_path got_period got_failure
  got_path="$(probe_field "$1" httpGet.path)"
  got_period="$(probe_field "$1" periodSeconds)"
  got_failure="$(probe_field "$1" failureThreshold)"
  if [ "$got_path" = "$2" ] && [ "$got_period" = "$3" ] && [ "$got_failure" = "$4" ]; then
    ok "$1：path=$got_path period=${got_period}s failureThreshold=$got_failure"
  else
    bad "$1：期望 path=$2 period=$3 failure=$4，实际 path=$got_path period=$got_period failure=$got_failure"
  fi
}
check_probe startupProbe   /health 2  30
check_probe livenessProbe  /health 10 3
check_probe readinessProbe /ready  3  2

# 语义检查：startup 的容忍窗口必须 >= liveness 的容忍窗口，否则 startup 形同虚设
ST_WIN=$((2 * 30)); LV_WIN=$((10 * 3))
if [ "$ST_WIN" -ge "$LV_WIN" ]; then
  ok "启动窗口 ${ST_WIN}s ≥ 重启窗口 ${LV_WIN}s（startup 确实在保护启动期）"
else
  bad "启动窗口 ${ST_WIN}s < 重启窗口 ${LV_WIN}s → startup 覆盖不了 liveness，配置矛盾"
fi

# ---- 2. startupProbe：启动期 liveness 不执行（⭐ 对照实验）----
hr
echo "[2/5] ⭐ startupProbe 对照实验（同样的慢启动，有/无 startup 差别多大）"
info "注入 20 秒启动延迟，分别看「去掉 startup」与「保留 startup」的结果"
info "（20s > liveness 的 3×10s=30s？不 —— 这里刻意用 20s 落在容忍窗口内，"
info " 再做 45s 的越界用例。本步骤跑 45s 越界版，证据最硬）"

# 反面：去掉 startupProbe + 45s 启动
K patch deployment llmops-api --type=json -p='[
  {"op":"remove","path":"/spec/template/spec/containers/0/startupProbe"},
  {"op":"replace","path":"/spec/template/spec/containers/0/env/1/value","value":"45"}
]' >/dev/null 2>&1
info "已去掉 startupProbe + 启动延迟 45s，等 100 秒观察是否被 liveness 误杀 ..."
sleep 100
BAD_RESTARTS=$(K get pods -l app=llmops-api \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{" "}{end}' 2>/dev/null \
  | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -rn | head -1)
if [ "${BAD_RESTARTS:-0}" -ge 1 ] 2>/dev/null; then
  ok "反面：无 startup 时慢启动被误杀（最高重启 ${BAD_RESTARTS} 次）→ 证明 startup 的必要性"
else
  bad "反面实验未复现误杀（重启 ${BAD_RESTARTS:-0} 次）→ 检查 liveness 容忍窗口是否被改大"
fi

# 正面：加回 startupProbe，启动延迟**保持不变**
K patch deployment llmops-api --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/startupProbe",
   "value":{"httpGet":{"path":"/health","port":"http","scheme":"HTTP"},
            "periodSeconds":2,"failureThreshold":30,"timeoutSeconds":1,"successThreshold":1}}
]' >/dev/null 2>&1
info "已加回 startupProbe（延迟仍 45s），等 120 秒观察是否零重启起来 ..."
sleep 120
NEW_POD=$(K get pods -l app=llmops-api \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null \
  | awk '$2==0{print $1}' | head -1)
if [ -n "$NEW_POD" ]; then
  READY=$(K get pod "$NEW_POD" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)
  ok "正面：有 startup 时同样 45s 启动零重启（pod=$NEW_POD ready=$READY）"
else
  bad "正面实验：未找到零重启的新 Pod"
fi

# 取证：启动期只有 startup 告警、没有 liveness 告警
info "取证 —— 事件里应只见 Startup probe failed，不见 Liveness probe failed 同期出现"
K get events --sort-by=.lastTimestamp 2>/dev/null \
  | grep -c "Startup probe failed" >/dev/null 2>&1 \
  && ok "事件中存在 Startup probe failed（探针确实在探）" \
  || info "事件已过期被回收（不影响结论）"

# 还原延迟与清单
K apply -f infra/k8s/app.yaml >/dev/null 2>&1
K rollout status deployment/llmops-api --timeout=120s >/dev/null 2>&1
ok "已还原清单（延迟归零 + 三探针齐全）"

# ---- 3. readinessProbe：摘流量但不重启 ----
hr
echo "[3/5] ⭐ readinessProbe：失败 → 摘流量（**不重启**）"
BEFORE_EP=$(K get endpoints llmops-api -o jsonpath='{.subsets[0].addresses[*].ip}' 2>/dev/null | wc -w)
BEFORE_IPS=$(K get endpoints llmops-api -o jsonpath='{.subsets[0].addresses[*].ip}' 2>/dev/null)
info "注入前端点：${BEFORE_IPS:-（空）}（共 $BEFORE_EP 个）"

POD0=$(K get pods -l app=llmops-api -o jsonpath='{.items[0].metadata.name}')
K exec "$POD0" -- touch /tmp/not-ready >/dev/null 2>&1
info "已对 $POD0 注入就绪失败，等 8 秒让探针连续失败 ..."
sleep 8

READY0=$(K get pod "$POD0" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)
REST0=$(K get pod "$POD0" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
PHASE0=$(K get pod "$POD0" -o jsonpath='{.status.phase}' 2>/dev/null)
AFTER_EP=$(K get endpoints llmops-api -o jsonpath='{.subsets[0].addresses[*].ip}' 2>/dev/null | wc -w)

if [ "$READY0" = "false" ] && [ "$PHASE0" = "Running" ]; then
  ok "Pod 仍是 Running 但 READY=false（=0/1）—— 这就是 D5 卡片点名的坑的形态"
else
  bad "Pod ready=$READY0 phase=$PHASE0（期望 ready=false 且 phase=Running）"
fi
if [ "$REST0" = "0" ]; then
  ok "退出码之外的关键断言：RESTARTS=0 → readiness 失败**不重启**容器"
else
  bad "RESTARTS=$REST0（期望 0，readiness 不应导致重启）"
fi
if [ "$AFTER_EP" -lt "$BEFORE_EP" ]; then
  ok "端点从 $BEFORE_EP 个减到 $AFTER_EP 个 → 失败 Pod 已被摘出负载均衡"
else
  bad "端点数未减少（注入前 $BEFORE_EP，注入后 $AFTER_EP）→ 摘除机制未生效"
fi

# 恢复
K exec "$POD0" -- rm -f /tmp/not-ready >/dev/null 2>&1
sleep 6
RECOVER_EP=$(K get endpoints llmops-api -o jsonpath='{.subsets[0].addresses[*].ip}' 2>/dev/null | wc -w)
if [ "$RECOVER_EP" -eq "$BEFORE_EP" ]; then
  ok "移除标记后端点恢复到 $RECOVER_EP 个（故障自愈）"
else
  bad "端点未恢复（期望 $BEFORE_EP，实际 $RECOVER_EP）"
fi

# ---- 4. 三种排错演练 ----
hr
echo "[4/5] 三种排错演练（各查一次，断言「诊断信息出现在该出现的地方」）"

# --- 4a 错 tag → ImagePullBackOff ---
info "4a · 镜像 tag 写错"
K create deployment d5-badtag --image=llmops-api:v9.9.9 >/dev/null 2>&1
sleep 15
ST=$(K get pods -l app=d5-badtag -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)
case "$ST" in
  ImagePullBackOff|ErrImagePull)
    ok "4a 错 tag → $ST（期望 ImagePullBackOff/ErrImagePull）" ;;
  *)
    bad "4a 错 tag → 状态是 '$ST'（期望 ImagePullBackOff）" ;;
esac
# 诊断信息必须在 events 里能找到「镜像名 + 失败」
PODA=$(K get pods -l app=d5-badtag -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if K describe pod "$PODA" 2>/dev/null | grep -qi "Failed to pull image\|ErrImagePull"; then
  ok "4a describe 的 Events 段给出拉取失败详情（可据此定位是 tag 还是仓库权限）"
else
  bad "4a describe 里找不到拉取失败事件"
fi
K delete deployment d5-badtag --wait=false >/dev/null 2>&1

# --- 4b 内存过小 → OOMKilled ---
info "4b · 内存 limit 过小（32Mi < 实测 ~40MB）"
K create deployment d5-oom --image=llmops-api:0.2.0 >/dev/null 2>&1
K set resources deployment/d5-oom --limits=memory=32Mi --requests=memory=32Mi >/dev/null 2>&1
sleep 25
OOMREASON=$(K get pods -l app=d5-oom \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].lastState.terminated.reason}{"\n"}{end}' 2>/dev/null \
  | grep -m1 OOMKilled)
OOMCODE=$(K get pods -l app=d5-oom \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].lastState.terminated.exitCode}{"\n"}{end}' 2>/dev/null \
  | grep -m1 '^137$')
if [ "$OOMREASON" = "OOMKilled" ]; then
  ok "4b 内存过小 → lastState.reason=OOMKilled"
else
  bad "4b 未观察到 OOMKilled（实际 '$OOMREASON'）"
fi
if [ "$OOMCODE" = "137" ]; then
  ok "4b 退出码 137（=128+9=SIGKILL）—— OOM 的判据"
else
  bad "4b 退出码不是 137（实际 '$OOMCODE'）"
fi
K delete deployment d5-oom --wait=false >/dev/null 2>&1

# --- 4c readiness 路径错 → Running 但不接流量 ---
info "4c · readiness 路径写错（/readiness 不存在）"
K create deployment d5-badready --image=llmops-api:0.2.0 >/dev/null 2>&1
K patch deployment d5-badready --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/readinessProbe",
   "value":{"httpGet":{"path":"/readiness","port":8000,"scheme":"HTTP"},
            "periodSeconds":3,"failureThreshold":2,"successThreshold":1,"timeoutSeconds":1}}
]' >/dev/null 2>&1
sleep 20
PHASE_C=$(K get pods -l app=d5-badready -o jsonpath='{.items[0].status.phase}' 2>/dev/null)
READY_C=$(K get pods -l app=d5-badready -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)
REST_C=$(K get pods -l app=d5-badready -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)
if [ "$PHASE_C" = "Running" ] && [ "$READY_C" = "false" ]; then
  ok "4c STATUS=Running 但 READY=false → 只看 STATUS 会漏判（本项就是验收目标）"
else
  bad "4c phase=$PHASE_C ready=$READY_C（期望 Running + false）"
fi
if [ "$REST_C" = "0" ]; then
  ok "4c RESTARTS=0 → 再次确认 readiness 失败不重启"
else
  bad "4c RESTARTS=$REST_C（期望 0）"
fi
PODC=$(K get pods -l app=d5-badready -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if K describe pod "$PODC" 2>/dev/null | grep -q "Readiness probe failed"; then
  ok "4c describe 里能看到 'Readiness probe failed'（定位此处即可发现路径写错）"
else
  bad "4c describe 里找不到 Readiness 失败事件"
fi
K delete deployment d5-badready --wait=false >/dev/null 2>&1

# ---- 5. 产出物清单 ----
hr
echo "[5/5] 产出物清单"
for f in infra/k8s/app.yaml app/main.py docs/decisions.md docs/kubectl-cheatsheet.md; do
  [ -f "$f" ] && ok "$f" || bad "$f 缺失"
done

# ---- 收尾还原 ----
hr
echo "[收尾] 确认集群回到干净状态"
K apply -f infra/k8s/app.yaml >/dev/null 2>&1
K rollout status deployment/llmops-api --timeout=120s >/dev/null 2>&1
LEFTOVER=$(K get deployments -o name 2>/dev/null | grep -cE 'd5-|broken|oom-|bad-ready' || true)
if [ "${LEFTOVER:-0}" -eq 0 ]; then
  ok "演练用 Deployment 已清理干净"
else
  info "⚠️ 还有 $LEFTOVER 个演练 Deployment 残留（--wait=false 后台删除中，稍后自消）"
fi
K get pods -l app=llmops-api

# ---- 汇总 ----
hr
echo " 结果：通过 $PASS 项，失败 $FAIL 项"
echo "============================================================="
[ "$FAIL" -eq 0 ] && { echo " ✅ D5 验收全部通过"; exit 0; } || { echo " ❌ 有失败项"; exit 1; }
