#!/usr/bin/env bash
# 把宿主机 docker 镜像灌进 kind 节点。
#
# 为什么不用 kind load docker-image：
#   本机 kind + containerd 2.3.4 下，kind 默认给 ctr 传 --all-platforms，
#   而 docker save 只存宿主机这一个平台，必然报
#     ctr: content digest sha256:...: not found
#   所以走 save -> cp -> import 三步，并固定 --platform linux/amd64。
#
# 两个必须知道的坑（2026-10-13 实测，都很容易浪费时间）：
#   1) 目标必须落节点 /root/，不能落 /tmp/
#      节点 /tmp 是 tmpfs 挂载，docker cp 写的是底层 rootfs，被 tmpfs 遮住，
#      容器里 ls 看不到，但退出码仍是 0 —— 静默失败。
#   2) docker save 前必须确认镜像真的下完了。
#      `timeout 150 docker pull ... | tail; echo $?` 取到的是 tail 的退出码，
#      pull 被 timeout 杀了也会显示成功。判断成功要看 docker images，不能看管道退出码。
#
# 用法：
#   load_image_to_kind.sh <镜像[:tag]> [节点名]
# 例：
#   load_image_to_kind.sh busybox:1.28
#   load_image_to_kind.sh registry.k8s.io/hpa-example:latest llmops-control-plane
#
# 注意：喂给 Deployment 的 image 字段一定要写全 tag（含 :latest），
# 否则 kubelet 匹配不到本地镜像，会去联网重拉。

set -euo pipefail

# 关掉 MSYS 的路径自动转换，否则容器内的 /root/xxx.tar 会被改写成 C:/Users/...
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

IMG="${1:-}"
NODE="${2:-llmops-control-plane}"

if [ -z "$IMG" ]; then
  echo "用法: $0 <镜像[:tag]> [节点名]" >&2
  exit 1
fi

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "ERROR: 宿主机没有镜像 $IMG —— 先把它 pull 完整（不要看管道退出码，用 docker images 确认）" >&2
  exit 1
fi

# tar 放系统临时目录，文件名把 / 和 : 换成 _ 以免路径歧义
SAFE="$(echo "$IMG" | tr '/:' '__')"
TAR="${TMPDIR:-/tmp}/${SAFE}.tar"
# Git Bash 的 /tmp 映射到 Windows 真实路径时 docker 也能认，但用显式绝对路径更稳
case "$TAR" in
  /tmp/*) TAR="C:/Users/lile2/AppData/Local/Temp/${SAFE}.tar" ;;
esac

echo "[1/3] docker save $IMG -> $TAR"
docker save "$IMG" -o "$TAR"

echo "[2/3] docker cp -> $NODE:/root/ （不要用 /tmp，那是 tmpfs 会被遮住）"
docker cp "$TAR" "$NODE:/root/${SAFE}.tar"

echo "[3/3] ctr images import --platform linux/amd64"
docker exec "$NODE" ctr -n k8s.io images import --platform linux/amd64 "/root/${SAFE}.tar"

# 收尾校验：从 CRI 视角确认真的能看见了
# crictl images 的输出是分列的（IMAGE / TAG / IMAGE ID / SIZE），并且会把 docker hub 短名
# 规范化成 docker.io/library/<name>（如 busybox:1.28 -> docker.io/library/busybox : 1.28），
# 所以不能拿原始字符串直接 grep。
REPO="${IMG%%:*}"
if [[ "$IMG" == *:* ]]; then
  TAG="${IMG##*:}"
else
  TAG="latest"
fi

# CRI 规范化规则：
#   - 无 '/'            -> docker.io/library/<name>
#   - 有 '/' 且首段无 '.' 无 ':' -> docker.io/<name>（如 user/repo）
#   - 否则（含注册表域名）        -> 原样（如 registry.k8s.io/hpa-example）
CRI_FORM="$REPO"
if [[ "$REPO" != *"/"* ]]; then
  CRI_FORM="docker.io/library/$REPO"
elif [[ "$REPO" != *.* && "$REPO" != *:* ]]; then
  CRI_FORM="docker.io/$REPO"
fi

CRI_LIST="$(docker exec "$NODE" crictl images 2>/dev/null || true)"
if echo "$CRI_LIST" | awk -v repo="$CRI_FORM" -v tag="$TAG" '$1==repo && $2==tag {f=1} END{exit !f}'; then
  echo "OK: $IMG 已在节点 $NODE（CRI 视角可见：$CRI_FORM:$TAG）"
elif echo "$CRI_LIST" | awk -v repo="$REPO" -v tag="$TAG" '$1==repo && $2==tag {f=1} END{exit !f}'; then
  echo "OK: $IMG 已在节点 $NODE"
else
  echo "WARN: import 报成功但 crictl images 里没找到 $IMG（期望 $CRI_FORM:$TAG），请人工核对" >&2
  exit 1
fi
