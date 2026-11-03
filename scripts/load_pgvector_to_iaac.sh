#!/usr/bin/env bash
# =============================================================================
# W5 D6 · 把 pgvector 镜像从现有集群节点拷进 IaC 集群节点
# -----------------------------------------------------------------------------
# 为什么需要这个脚本（Docker 29 + kind 已知 bug，kind issue #3795/#4066）：
#   · Docker 29 用 containerd image store 后，`docker save` 导出的 OCI 多平台
#     index 引用了未导出的 blob（amd64 宿主上 arm64 manifest 没有 blob）
#   · `kind load docker-image` / `kind load image-archive` / terraform 的
#     `kind_load` 资源底层全走 `ctr images import --all-platforms` → 全部失败
#     报 `ctr: content digest sha256:...: not found`
#   · 节点直连 Docker Hub 拉镜像也不通：kind 把宿主的 HTTP_PROXY=127.0.0.1:xxxx
#     带进节点容器，容器内 127.0.0.1 不是宿主 → proxyconnect refused
#
# 本脚本的路线（绕开上面两个坑）：
#   老节点 ctr export --platform linux/amd64（单平台，blob 齐全）
#   → docker cp 中转到宿主 → docker cp 进新节点
#   → 新节点 ctr import --platform linux/amd64
#
# 用法：bash scripts/load_pgvector_to_iaac.sh [源集群名] [目标集群名]
#   默认：源 llmops，目标 llmops-iaac
# =============================================================================
set -euo pipefail

# Git Bash 的 MSYS 路径转换会把 /pgvector-export.tar 这类绝对路径改写成
# Windows 路径传给 docker exec → 节点内路径失效。本脚本所有 docker exec 都要禁用。
export MSYS_NO_PATHCONV=1

SRC_CLUSTER="${1:-llmops}"
DST_CLUSTER="${2:-llmops-iaac}"
IMAGE="docker.io/pgvector/pgvector:pg16"
HOST_TAR="$(dirname "$0")/../infra/terraform/pgvector-amd64.tar"

echo "[1/4] 从源节点 ${SRC_CLUSTER}-control-plane 导出 ${IMAGE}（linux/amd64）"
docker exec "${SRC_CLUSTER}-control-plane" \
  ctr --namespace=k8s.io images export --platform linux/amd64 /pgvector-export.tar "${IMAGE}"

echo "[2/4] 拷到宿主：${HOST_TAR}"
docker cp "${SRC_CLUSTER}-control-plane:/pgvector-export.tar" "${HOST_TAR}"

echo "[3/4] 拷进目标节点 ${DST_CLUSTER}-control-plane"
docker cp "${HOST_TAR}" "${DST_CLUSTER}-control-plane:/pgvector-import.tar"

echo "[4/4] 目标节点导入（linux/amd64）"
docker exec "${DST_CLUSTER}-control-plane" \
  ctr --namespace=k8s.io images import --platform linux/amd64 /pgvector-import.tar

echo ""
echo "✅ 完成。验证：docker exec ${DST_CLUSTER}-control-plane crictl images | grep pgvector"
echo "   如果 Pod 还在 ImagePullBackOff，kubelet 会自动重试并用上这个镜像，无需重启。"
