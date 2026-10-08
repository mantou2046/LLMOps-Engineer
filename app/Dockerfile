# syntax=docker/dockerfile:1.7
# =============================================================================
# 多阶段构建（Multi-Stage Build）· 生产镜像
# -----------------------------------------------------------------------------
# 八周计划 W1 D2 产出物。三条核心实践（对应 TechWorld with Nana BP4/BP6/BP7）：
#   · BP4 优化层缓存顺序  → 先 COPY requirements 并装依赖，最后 COPY 源码
#   · BP6 多阶段构建      → builder 装依赖，runtime 只带走 venv，不留编译器
#   · BP7 非 root 运行     → 建 appuser，USER 切换，容器内 whoami ≠ root
#
# 构建（上下文 = app/ 的父目录，即仓库根）：
#   docker build -f app/Dockerfile -t llmops-api:0.1.0 .
# 体积对比见 README「镜像体积对比」一节。
#
# ── 为什么用 alpine 而不是 slim-bookworm（2026-10-08 实测决策）─────────────
# 验收指标要求最终镜像 < 200MB，而 python:3.12.7-slim-bookworm 基础层就占 188MB ——
# 物理上不可能达标。换 python:3.12-alpine（83.6MB）后实测 145MB，留出充足余量。
# 前提已验证：本项目全部带 C 扩展的依赖（uvloop / pydantic-core / httptools /
# watchfiles）都有 musllinux 预编译 wheel，**不需要在镜像里装 gcc/build-base** ——
# 否则为编译装进去的工具链会把省下的体积全吃回去。
# ⚠️ 副作用：alpine 用 musl libc 而非 glibc。若后续引入只在 glibc 下发包的依赖
#    （典型如某些版本的 oracledb、部分科学计算包），需回退本文件至 slim 变体。
# =============================================================================

# -----------------------------------------------------------------------------
# 阶段一 · builder：装依赖（这里可以很大，反正不进最终镜像）
# -----------------------------------------------------------------------------
FROM python:3.12-alpine AS builder

# 固定版本 tag（BP2：不用 latest）—— 保证构建可重现。
# Python 3.12 而非 3.13：生态兼容性更稳。

# ---- apk 换国内源 ----
# ⚠️ 实测教训（2026-10-08）：Debian 侧不换源时构建卡在 deb.debian.org（124 秒才下到第 14 个包）。
# 本机实测直连速度：腾讯云 457 KB/s > 阿里云 193 > 中科大 138 > 清华 86 KB/s。
# 默认阿里云（mirrors.cloud.tencent.com 不提供 alpine 仓库，debian 侧用的才是它）。
# 境外构建可加 --build-arg APK_MIRROR=dl-cdn.alpinelinux.org 改回官方源。
ARG APK_MIRROR=mirrors.aliyun.com
RUN set -eux; \
    sed -i "s|dl-cdn.alpinelinux.org|${APK_MIRROR}|g" /etc/apk/repositories; \
    cat /etc/apk/repositories

# 本阶段**故意不装 gcc/musl-dev** —— 实测所有依赖都有 musllinux wheel，无需现场编译。
# 如果哪天某个依赖开始走 sdist，这里会立刻报错而不是悄悄变慢，属于理想的失败方式。

WORKDIR /build

# 把虚拟环境建在固定路径，方便 runtime 段整体拷贝
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# ---- BP4：依赖声明先拷，依赖安装单独成层 ----
# 源码改了不会让这一层失效；只有 requirements.txt 变了才重新装依赖。
COPY requirements.txt ./
# pip 同样换国内源（容器内网络不走宿主机代理，直连 PyPI 会很慢）
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
# ⚠️ 清理必须写在**同一条 RUN** 里（2026-10-08 踩坑实录）：
#    最初把「删 pip」放到 runtime 阶段，结果镜像从 270MB 涨到 284MB ——
#    因为 COPY venv 那层已经含 pip，后面的 RUN rm 只是加了个「删除标记」，
#    底下那层的数据仍在镜像里（overlayfs 是叠加不是覆盖）。
#    正确做法：在 builder 里装完就删，这样 COPY 过去的 venv 天生就是精简的。
RUN pip install --no-cache-dir -i ${PIP_INDEX} --upgrade pip \
 && pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt \
 && SP=${VIRTUAL_ENV}/lib/python3.12/site-packages \
 && rm -rf $SP/pip $SP/pip-*.dist-info \
           $SP/setuptools $SP/setuptools-*.dist-info \
           $SP/wheel $SP/wheel-*.dist-info \
           $SP/pkg_resources \
 && find ${VIRTUAL_ENV} -type d -name '__pycache__' -prune -exec rm -rf {} +
# --no-cache-dir：不在镜像里留 pip 下载缓存（不装依赖的层不需要它）
# 删 pip/setuptools/wheel：容器里不会再装包，留着纯属浪费（实测省约 15MB）

# -----------------------------------------------------------------------------
# 阶段二 · runtime：只带运行需要的东西
# -----------------------------------------------------------------------------
FROM python:3.12-alpine AS runtime

# 元信息（docker inspect 可见，便于追溯）
LABEL org.opencontainers.image.title="llmops-engineer-api" \
      org.opencontainers.image.description="资金/财务制度知识库智能问答与评估平台 · API 服务" \
      org.opencontainers.image.source="https://github.com/mantou2046/LLMOps-Engineer" \
      org.opencontainers.image.licenses="MIT"

# ⚠️ 这里**不装任何包**（连 curl 都不装）—— 详见下方 HEALTHCHECK 注释。
#    alpine 自带 busybox，含 wget / nc、addgroup / adduser，够用。

# ---- BP7：建非 root 用户 ----
# ⚠️ alpine 是 busybox 版 addgroup/adduser，参数与 Debian 的 shadow-utils 不同：
#    · 只支持短选项，不认 --gid / --uid / --create-home 这类长选项
#    · 建用户与建组一步完成（-G 指定属组、-S 建系统用户、-D 不设密码）
#    · shell 用 /sbin/nologin，创建 home 用 -h（alpine 下该目录默认不建，用 -H 可显式不建）
#    固定 UID/GID 1001 便于 K8s securityContext 对齐。
RUN addgroup -g 1001 appuser \
    && adduser -u 1001 -G appuser -S -D -H -s /sbin/nologin appuser

WORKDIR /app

# ---- BP6：只从 builder 拷 venv，编译器与构建工具全部留在上一阶段 ----
# 本方案里 builder 根本没装编译器（依赖全是预编译 wheel），
# 但多阶段依然有价值：pip 缓存、源码构建目录、venv 之外的临时物都不进最终镜像。
COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv

# 注：venv 的精简（删 pip / setuptools / __pycache__）已在 **builder 阶段**完成。
# 这里**不能再删一次** —— 那只会加一层「删除标记」，底层数据仍在，体积反而变大（见上方踩坑注释）。

# 应用代码：精确 COPY，不用 COPY . .（配合 .dockerignore 双保险）
COPY --chown=appuser:appuser app/main.py ./main.py

# 切到非 root
USER appuser

EXPOSE 8000

# 健康检查：容器自身可自证存活（K8s 里会被 liveness/readiness 探针取代）
# ⚠️ alpine 无 curl，用 busybox 自带的 wget：
#    · -q 静默（否则健康检查日志会刷屏）
#    · -T 3 超时 3 秒，与原先 --timeout=3s 对齐
#    · --spider 只发 HEAD 不发 body，最省事
#    · --no-check-certificate 不需要：这里是 http 自连，无 TLS
#    ⚠️ 如果为了「让 curl 可用」而 apk add curl，会带回 ca-certificates + 约 4~8MB 体积，
#       与 < 200MB 的目标背道而驰 —— 这就是本文件不装 curl 的原因。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -q -T 3 --spider http://localhost:8000/health || exit 1

# 生产不用 --reload；worker 数由编排层决定，不写死
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
