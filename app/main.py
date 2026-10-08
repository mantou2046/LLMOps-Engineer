"""LLMOps 作品集 · API 服务层（最小可运行骨架）

W1 D2 阶段目标：先让「容器化这件事」跑通 —— 一个能被构建、能起、能被探活的
最小服务。真正的 LLM 调用、结构化输出、重试与流式在 W2 补齐。

设计上刻意保持极简：
  · 只依赖 fastapi / pydantic，不引入任何 LLM SDK → 镜像小、启动快
  · /health 不碰任何外部依赖 → 供 Docker HEALTHCHECK 与 K8s liveness 使用
  · /ready  预留位（W2 接 pgvector 后改查数据库连通性）
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI

APP_VERSION = os.getenv("APP_VERSION", "0.1.0")

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
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["ops"])
def ready() -> dict:
    """就绪探针：W2 接入 pgvector 后改为探测数据库连通性。

    现在返回静态就绪 —— 阶段内没有外部依赖，占位以便编排层先行接入。
    """
    return {"status": "ready", "checks": {"database": "not-wired-yet"}}


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
