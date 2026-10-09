#!/usr/bin/env bash
# =============================================================================
# 在 kind 上装 ingress-nginx —— W1 D4 第 2 步
# -----------------------------------------------------------------------------
# ⚠️⚠️ 这个脚本会 **删掉并重建集群**！
#
# 原因：kind 的 extraPortMappings（宿主 80 → 节点 30080）**只在创建集群时生效**。
#       当前集群 llmops 是 D3 用默认参数建的，没有这个端口映射，
#       所以即使装上 ingress-nginx，宿主机的 80 端口也访问不到。
#
# 重建成本很低（镜像 load + apply 都有现成脚本），但会丢掉当前 PVC 里的数据。
# 如需保留 postgres 数据，先手工备份：
#   kubectl exec statefulset/postgres -- pg_dump -U llmops llmops > backup.sql
#
# 用法：
#   cd /e/Projects/LLMOps-Engineer
#   bash scripts/setup_ingress_kind.sh          # 会先问一次确认
#   bash scripts/setup_ingress_kind.sh --yes    # 跳过确认（CI 用）
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER="llmops"
NS_CTRL="ingress-nginx"
NODE="llmops-control-plane"
KIND_BIN="$(command -v kind || echo /c/Users/lile2/bin/kind.exe)"
KUBECTL_BIN="$(command -v kubectl || echo "/c/Users/lile2/AppData/Local/Programs/DockerDesktop/resources/bin/kubectl")"
MANIFEST="infra/k8s/kind-ingress-config.yaml"
CONTROLLER_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml"
# ⚠️ 注册表里那个真能出墙的代理（沙箱 env 的 HTTPS_PROXY 只放行白名单，
#    拦 raw.githubusercontent.com）。端口为 9527，见用户级 MEMORY.md。
REGISTRY_PROXY="${REGISTRY_PROXY:-http://127.0.0.1:9527}"
CONTROLLER_IMG="registry.k8s.io/ingress-nginx/controller:v1.15.1"
CERTGEN_IMG="registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.6.9"

hr() { echo "-------------------------------------------------------------"; }

echo "============================================================="
echo " 在 kind 上装 ingress-nginx"
echo "============================================================="
hr

if [ ! -f "$MANIFEST" ]; then
  echo "❌ 找不到 $MANIFEST"; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "❌ Docker 引擎没跑"; exit 1
fi

# ---- 风险提示 ----
echo "⚠️  即将执行："
echo "      kind delete cluster --name $CLUSTER"
echo "      kind create cluster --name $CLUSTER --config $MANIFEST"
echo
echo "    这会删除当前集群里的一切（含 postgres 的 PVC 数据）。"
echo
if [ "${1:-}" != "--yes" ]; then
  read -r -p "确认继续？(yes/no) " REPLY
  if [ "$REPLY" != "yes" ]; then
    echo "已取消。"
    exit 0
  fi
fi

# ---- 1. 重建集群 ----
hr
echo "[1/4] 删除旧集群"
"$KIND_BIN" delete cluster --name "$CLUSTER" 2>&1 | tail -3

echo
echo "[2/4] 用 $MANIFEST 重建（带 extraPortMappings）"
"$KIND_BIN" create cluster --name "$CLUSTER" --config "$MANIFEST" 2>&1 | tail -8
"$KUBECTL_BIN" config use-context "kind-$CLUSTER" >/dev/null 2>&1
echo "     context → $("$KUBECTL_BIN" config current-context)"

# 重建后本地镜像全丢，必须重新 load
echo
echo "     ⚠️ 集群重建 → 节点镜像清空，重新载入 llmops-api:0.1.0"
"$KIND_BIN" load docker-image llmops-api:0.1.0 --name "$CLUSTER" 2>&1 | tail -2

# ---- 2. 装控制器 ----
hr
echo "[3/4] 安装 ingress-nginx 控制器"
echo
echo "     ⚠️ 关键一步：两个坑必须先绕开（都是实测踩过的）"
echo "        ① 沙箱 env 代理拦 raw.githubusercontent.com → 显式走 $REGISTRY_PROXY 下载"
echo "        ② 节点内 containerd 继承宿主代理（127.0.0.1 指向节点自己）→ 清掉"
echo

# ---- 3a. 清掉 containerd 的继承代理（否则镜像永远 ErrImagePull）----
echo "     [3a] 给节点 containerd 加 no-proxy drop-in ..."
docker exec "$NODE" sh -c '
mkdir -p /etc/systemd/system/containerd.service.d
cat > /etc/systemd/system/containerd.service.d/99-no-proxy.conf << "EOF"
# 清掉从宿主继承的沙箱代理：节点内部的 127.0.0.1 指向节点自己，
# 用宿主代理只会 connection refused。镜像已本地 import，无需回源。
[Service]
Environment=
Environment="NO_PROXY=*"
Environment="no_proxy=*"
EOF
systemctl daemon-reload
systemctl restart containerd
' 2>&1 | tail -2
sleep 6
echo "        containerd: $(docker exec "$NODE" systemctl is-active containerd 2>&1)"

# ---- 3b. 下载清单（走注册表代理，绕过沙箱代理）----
MANIFEST_LOCAL="infra/k8s/ingress-nginx/deploy-upstream.yaml"
if [ ! -f "$MANIFEST_LOCAL" ]; then
  echo "     [3b] 本地无清单，走 $REGISTRY_PROXY 下载 ..."
  mkdir -p "$(dirname "$MANIFEST_LOCAL")"
  if ! curl -s -x "$REGISTRY_PROXY" -o "$MANIFEST_LOCAL" -m 60 -f "$CONTROLLER_URL"; then
    echo "        ❌ 下载失败。手动："
    echo "           curl -x $REGISTRY_PROXY -o $MANIFEST_LOCAL $CONTROLLER_URL"
    exit 1
  fi
  echo "        下载完成（$(wc -c < "$MANIFEST_LOCAL") bytes）"
  echo "        ⚠️ 还需手工打两处补丁（Service→NodePort / 去掉 @sha256 钉死），"
  echo "           参考 git 历史或 docs/decisions.md D11。"
else
  echo "     [3b] 使用仓库内已打补丁的清单：$MANIFEST_LOCAL"
fi

# ---- 3c. 拉镜像到宿主（宿主能走代理出网，节点不能）----
echo "     [3c] 拉取两个镜像到宿主 ..."
for img in "$CONTROLLER_IMG" "$CERTGEN_IMG"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img" 2>&1 | tail -1
done

# ---- 3d. 导入节点（用 D9 的 ctr --platform 路径，绕开 multi-arch + 代理）----
echo "     [3d] 导入节点 ..."
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
for img in "$CONTROLLER_IMG" "$CERTGEN_IMG"; do
  fname=$(echo "$img" | tr '/:' '__').tar
  win_tar="$(cygpath -w "$REPO_ROOT" 2>/dev/null || echo "$REPO_ROOT")\\$fname"
  docker save "$img" -o "$win_tar" 2>&1 | tail -1
  docker cp "$win_tar" "$NODE:/$fname" 2>&1
  docker exec "$NODE" ctr -n k8s.io images import --platform linux/amd64 "/$fname" 2>&1 | tail -1
  rm -f "$fname"; docker exec "$NODE" rm -f "/$fname"
done

echo "     [3e] 应用清单 ..."
"$KUBECTL_BIN" apply -f "$MANIFEST_LOCAL" 2>&1 | tail -3

echo
echo "     等待控制器就绪 ..."
"$KUBECTL_BIN" -n "$NS_CTRL" rollout status deploy/ingress-nginx-controller --timeout=180s 2>&1 \
  || echo "     ⚠️ 未在 180s 内 Ready，继续（下面会校验）"

# ---- 3. 校验 ----
hr
echo "[4/4] 校验"
"$KUBECTL_BIN" get pods -n "$NS_CTRL" 2>&1 | head -10
echo
if "$KUBECTL_BIN" get ingressclass nginx >/dev/null 2>&1; then
  echo "  ✅ IngressClass 'nginx' 已就绪"
  echo
  echo "  下一步："
  echo "      kubectl apply -f infra/k8s/config.yaml"
  echo "      kubectl apply -f infra/k8s/app.yaml"
  echo "      kubectl apply -f infra/k8s/postgres.yaml   # 需先 load pgvector 镜像"
  echo "      kubectl apply -f infra/k8s/ingress.yaml"
  echo "      curl -H 'Host: llmops.local' http://localhost/health"
  echo
  echo "  💡 想用域名访问，给 hosts 加一行（管理员权限）："
  echo "      127.0.0.1  llmops.local"
  exit 0
else
  echo "  ❌ IngressClass 'nginx' 没出现 —— 排查："
  echo "      kubectl -n $NS_CTRL get pods"
  echo "      kubectl -n $NS_CTRL describe pod -l app.kubernetes.io/component=controller"
  exit 1
fi
