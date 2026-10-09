"""LLMOps 作品集 · API 服务层（最小可运行骨架）

W1 D2 阶段目标：先让「容器化这件事」跑通 —— 一个能被构建、能起、能被探活的
最小服务。真正的 LLM 调用、结构化输出、重试与流式在 W2 补齐。

设计上刻意保持极简：
  · 只依赖 fastapi / pydantic，不引入任何 LLM SDK → 镜像小、启动快
  · /health 不碰任何外部依赖 → 供 Docker HEALTHCHECK 与 K8s liveness 使用
  · /ready  预留位（W2 接 pgvector 后改查数据库连通性）

W1 D5 补充（三种探针的演示能力）：
  探针要能验证，应用就得有「可观测的状态变化」。加两个**默认关闭**的旋钮：
  · STARTUP_DELAY_SECONDS —— 模拟慢启动，让 startupProbe 有真实价值可演示
  · /ready 检查一个「不健康标记文件」 —— 不重启容器即可把 Pod 从 EndpointSlice
    摘掉，用于演示「readiness 失败 ≠ 容器被杀」这个最容易误判的区别
  两者默认都是「不影响正常行为」，生产语义不变。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

APP_VERSION = os.getenv("APP_VERSION", "0.1.0")

# ---- D5 演示旋钮①：模拟慢启动 ----------------------------------------------
# 单位秒，默认 0 = 行为与之前完全一致。
# 用于演示 startupProbe：注入一个较长延迟后，liveness 的 initialDelaySeconds
# 不够用会误杀，而 startupProbe 能「占住」这段启动期不判失败。
STARTUP_DELAY_SECONDS = int(os.getenv("STARTUP_DELAY_SECONDS", "0"))

# ---- D5 演示旋钮②：就绪标记文件 ---------------------------------------------
# 该文件**存在**时 /ready 返回 503。默认路径在容器可写目录下。
# 演示方式（无需重启容器）：
#   kubectl exec deploy/llmops-api -- touch /tmp/not-ready
#   kubectl exec deploy/llmops-api -- rm -f /tmp/not-ready
NOT_READY_FILE = Path(os.getenv("NOT_READY_FILE", "/tmp/not-ready"))  # noqa: S108

# 进程启动时刻，用于 /ready 报告「已运行多久」
_STARTED_AT = time.time()

# 慢启动：只在模块导入时阻塞一次。刻意放在这里（而不是 lifespan 钩子里），
# 因为探针打的是 HTTP 端口，而 uvicorn 要等 import 完才 listen ——
# 这样「进程存在但端口没开」的窗口才是真实的慢启动形态。
if STARTUP_DELAY_SECONDS > 0:
    time.sleep(STARTUP_DELAY_SECONDS)

app = FastAPI(
    title="LLMOps Engineer 作品集 API",
    description="资金/财务制度知识库智能问答与评估平台 · 服务层",
    version=APP_VERSION,
)


@app.get("/health", tags=["ops"])
def health() -> dict:
    """存活探针：只回答「进程活着吗」，不查任何外部依赖。

    刻意不解耦的理由：liveness 探针若依赖数据库，数据库抖动会导致容器被反复重启，
    把「外部依赖故障」放大成「服务不可用」。外部依赖的检查属于 readiness。

    ⚠️ 这里**故意不看** NOT_READY_FILE —— 那是 readiness 的语义。
    liveness 失败会重启容器，把「暂时不该接流量」升级成「重启」是错的。
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["ops"])
def ready():
    """就绪探针：W2 接入 pgvector 后改为探测数据库连通性。

    现在检查两件事，都无需外部依赖：
      1. 启动延迟是否已过（进程早已 import 完成，这里恒真，保留作语义完整）
      2. 是否存在「不健康标记文件」—— 存在则返回 503，让 EndpointSlice
         控制器把本 Pod 的 IP 摘掉

    ⚠️ 与 /health 的关键区别：这里返回 503 **不会**导致容器被杀，
    只是**不再给它转发流量**。这正是 D5 卡片点名的那个坑 ——
    Pod 显示 Running，但服务永远 504/无响应，因为它从未进过 Endpoints。
    """
    if NOT_READY_FILE.exists():
        return JSONResponse(
            status_code=503,
            content={
                "status": "not-ready",
                "reason": f"存在标记文件 {NOT_READY_FILE}",
                "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            },
        )
    return {
        "status": "ready",
        "checks": {"database": "not-wired-yet"},
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
    }


@app.get("/", tags=["meta"])
def root() -> dict:
    """给浏览者的门面信息，顺带自证「容器内跑的是非 root 用户」。"""
    return {
        "service": "llmops-engineer-api",
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "user": os.getenv("USER") or os.getenv("USERNAME") or "unknown",
        "docs": "/docs",
    }
