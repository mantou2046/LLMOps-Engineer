"""LLMOps 作品集 · API 服务层（最小可运行骨架）

W1 D2 阶段目标：先让「容器化这件事」跑通 —— 一个能被构建、能起、能被探活的
最小服务。

W2 D8：LLM 调用 / 重试 / 指数退避 / 超时 / 流式 / 结构化输出已补齐，
实现拆到 app/llm.py（策略层），本文件只留 /chat 的协议层（校验 / 组包 / 错误翻译）。
拆法理由见 app/llm.py 文件头。

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

W2 D7 补充（HPA 扩缩容的演示能力）：
  · ENABLE_LOAD_ENDPOINT —— 开启后 /work?seconds=N 在容器内空转 N 秒烧 CPU。
    同样**默认关闭**：探针端点太轻，压不出 HPA，需要一个可控的 CPU 负载源。

W2 D8 补充（服务化）：
  · /chat —— 调上游模型（OpenAI 兼容协议），支持流式（SSE）与结构化输出（JSON mode）
  · 重试 / 指数退避 / 超时 三项策略在 app/llm.py；本文件只管协议与错误翻译
  · /healthz、/readyz —— K8s 惯例的别名入口（原 /health、/ready 保留不动）
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import llm
import observability
from llm import ChatStream, UpstreamError, sse

log = logging.getLogger("llmops.api")

# ---- W2 D11 修复：配置日志输出（此前 log.info 全部静默丢失） -------------------
# ⚠️ 原来只有 getLogger()、没有 basicConfig()：Python 的 root logger 默认用
#    lastResort handler（级别 WARNING），于是所有 log.info() 都被吞掉 ——
#    OBSERVABILITY=log 档本该打印的「trace 摘要」一条都看不到。
#    D11 全链路复跑时发现：dev 打 /chat 后 kubectl logs 里没有任何 trace 行。
#    级别取 LOG_LEVEL（由 ConfigMap 注入：dev=DEBUG / prod=INFO）。
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)

APP_VERSION = os.getenv("APP_VERSION", "0.5.2")

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

# ---- D7 演示旋钮③：CPU 压测端点 ---------------------------------------------
# 供 HPA（水平自动扩缩）演示用：/work?seconds=0.2 在容器内空转 N 秒，把 CPU 打上去。
# 这样压测就**不依赖外部负载发生器是否够力**，负载量完全可控、可复现。
#
# ⚠️ 默认**关闭**（ENABLE_LOAD_ENDPOINT 不为 "1" 时该路由返回 404）。
#    一个「帮忙烧 CPU」的端点在生产里就是现成的 DoS 放大器 ——
#    与 D5 的两个旋钮同理：演示能力必须默认关闭、显式开启。
ENABLE_LOAD_ENDPOINT = os.getenv("ENABLE_LOAD_ENDPOINT", "0") == "1"

# /work 单次最长空转秒数上限：防止误用（如 seconds=99999）把节点拖垮
MAX_BURN_SECONDS = 5.0

# 进程启动时刻，用于 /ready 报告「已运行多久」
_STARTED_AT = time.time()

# 慢启动：只在模块导入时阻塞一次。刻意放在这里（而不是 lifespan 钩子里），
# 因为探针打的是 HTTP 端口，而 uvicorn 要等 import 完才 listen ——
# 这样「进程存在但端口没开」的窗口才是真实的慢启动形态。
if STARTUP_DELAY_SECONDS > 0:
    time.sleep(STARTUP_DELAY_SECONDS)

# 启动即报告上游模式：/chat 是 mock 还是真实上游，是排查「为什么回复像演示」的第一现场。
# 不放日志而靠读代码判断，排障时多绕一圈。
log.info(
    "LLM 上游模式 = %s%s",
    llm.LLM_MODE,
    "（本地模拟，不调用真实模型；上生产前切 openai）" if llm.LLM_MODE == "mock" else "（直连真实上游）",
)
if llm.LLM_MODE == "openai":
    for _name, _tier in llm.TIERS.items():
        log.info(
            "  档位 %s：model=%s base_url=%s api_key=%s",
            _name,
            _tier.model or "未设",
            _tier.base_url or "默认",
            "已配置" if _tier.api_key else "未配置",
        )

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """进程级生命周期钩子。

    W2 D9：关闭前把 Langfuse 队列里还没发出去的 trace flush 掉。
    ⚠️ 不这样做，最后一批 trace 会随进程消失 ——「明明调了接口面板上却没有」
       十有八九就是这个原因。flush 失败绝不上抛：观测不该让优雅退出失败。
    """
    yield
    observability.flush()


app = FastAPI(
    title="LLMOps Engineer 作品集 API",
    description="资金/财务制度知识库智能问答与评估平台 · 服务层",
    version=APP_VERSION,
    lifespan=_lifespan,
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
        "checks": {
            "database": "not-wired-yet",
            # W2 D8：/chat 的上游模式与配置完整性。只报告、不据此摘流量 ——
            # 「配置没填对」该由告警发现，不是让 Pod 退出 Endpoints 把故障放大。
            "chat": {"mode": llm.LLM_MODE, "configured": llm.chat_configured()},
            # W2 D9：观测状态。同样只报告不摘流量 ——
            # ⚠️ Langfuse 挂了不该让本 Pod 退出 Endpoints：观测的可靠性等级低于业务，
            #    把它写进 readiness 门槛，等于把「面板看不了」放大成「服务不可用」。
            "observability": observability.status(),
        },
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
    }


# ---- K8s 惯例的 z 后缀入口 -------------------------------------------------
# /health、/ready 仍在用（Dockerfile HEALTHCHECK、chart 三探针的 httpGet.path），
# 路径不能改；/healthz、/readyz 是 K8s 生态更常见的写法，作为别名补上 ——
# 一份实现、两套入口，add_api_route 不复制逻辑。
app.add_api_route("/healthz", health, methods=["GET"], tags=["ops"])
app.add_api_route("/readyz", ready, methods=["GET"], tags=["ops"])


@app.get("/work", tags=["demo"])
def work(seconds: float = 0.2):
    """CPU 压测端点（**默认关闭**，见 ENABLE_LOAD_ENDPOINT）。

    在容器内空转 `seconds` 秒纯 Python 计算，用来把该 Pod 的 CPU 使用率推高，
    从而让 HPA 观察到「实际利用率 > 目标利用率」并触发扩容。

    与 /health、/ready 的关系：这两个探针端点几乎不吃 CPU，
    所以**光靠它们压不出 HPA**（探针请求太轻，利用率涨不到阈值）。
    这正是要单独造一个可控 CPU 负载的原因。

    ⚠️ 空转用的是 `time.perf_counter()` 而不是 `time.time()` ——
    后者会受系统时钟调整影响，压测时长会飘。
    """
    if not ENABLE_LOAD_ENDPOINT:
        # 用 404 而不是 403：不暴露「这里本来有个端点」的信息
        raise HTTPException(status_code=404, detail="Not Found")

    burn = max(0.0, min(seconds, MAX_BURN_SECONDS))
    deadline = time.perf_counter() + burn
    spins = 0
    while time.perf_counter() < deadline:
        spins += 1

    return {
        "burned_seconds": burn,
        "spins": spins,
        "requested_seconds": seconds,
        "clamped": seconds != burn,
        "pod": os.getenv("HOSTNAME", "unknown"),
    }


# ============================================================================
# W2 D8 · /chat：调上游模型（重试 / 指数退避 / 超时 / 流式 / 结构化输出）
# ============================================================================
# 策略实现全在 app/llm.py，这里只做协议层：校验 → 调用 → 组包（JSON 或 SSE）。
# 拆开的原因：llm.py 的变更是「策略问题」，本文件的变更是「接口问题」，混在一起
# 会互相拖累。


class Message(BaseModel):
    """一条对话消息。字段名与 OpenAI chat 协议对齐，不自定义方言。"""

    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1, description="消息正文")


class ChatRequest(BaseModel):
    """POST /chat 请求体 —— 四个业务字段正好对上 W2 D8 的四项「做」。"""

    messages: list[Message] = Field(..., min_length=1, description="对话历史，至少一条")
    stream: bool = Field(default=False, description="true → SSE 流式响应；false → 一次性 JSON")
    structured: bool = Field(default=False, description="true → 要求上游输出单个 JSON 对象（response_format=json_object）")
    model: Literal["primary", "cheap"] = Field(default="primary", description="模型档位：primary=主模型，cheap=便宜分流模型")
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=1024, ge=1, le=8192)


def _attempt_headers(
    attempts: int,
    retries: int,
    degraded: bool = False,
    degraded_from: str | None = None,
) -> dict[str, str]:
    """重试次数写进响应头：curl -i 一眼看到「自动重试过」，不必解析 body。
    响应体里同样带 attempts/retries —— 双通道可查，别只在一边。
    W2 D10：降级同样上头（X-LLM-Degraded），运维一眼可辨这单走的是备用通道。"""
    headers = {"X-Upstream-Attempts": str(attempts), "X-Upstream-Retries": str(retries)}
    headers.update(_degraded_headers(degraded, degraded_from))
    return headers


def _degraded_headers(degraded: bool, degraded_from: str | None) -> dict[str, str]:
    """降级标记头：未降级不发（避免给正常请求加噪音头）。"""
    if not degraded:
        return {}
    headers = {"X-LLM-Degraded": "true"}
    if degraded_from:
        headers["X-LLM-Degraded-From"] = degraded_from
    return headers


def _chat_502(exc: UpstreamError) -> HTTPException:
    """上游故障统一转 502 —— 语义是「我们没问题，上游有问题」，
    与 500「我们自己的问题」区分开，监控告警该分流处理。"""
    return HTTPException(
        status_code=502,
        detail={"message": exc.message, "upstream": exc.upstream, "status": exc.status},
    )


@app.post("/chat", tags=["llm"])
def chat(req: ChatRequest) -> Response:
    """调上游模型。策略在 app/llm.py；这里只做协议与错误翻译。"""
    log.info(
        "POST /chat stream=%s structured=%s tier=%s messages=%d",
        req.stream, req.structured, req.model, len(req.messages),
    )
    payload = [m.model_dump() for m in req.messages]

    if req.stream:
        try:
            stream = llm.open_stream(
                messages=payload,
                tier_name=req.model,
                structured=req.structured,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            )
        except UpstreamError as exc:
            raise _chat_502(exc) from exc
        return StreamingResponse(
            _sse_body(stream, structured=req.structured),
            media_type="text/event-stream",
            # X-Accel-Buffering=no：反向代理层别缓存 SSE（否则 delta 被攒成一坨才到客户端）
            headers={
                **_attempt_headers(stream.attempts, stream.retries),
                **_degraded_headers(stream.degraded, stream.degraded_from),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        out = llm.chat(
            messages=payload,
            tier_name=req.model,
            structured=req.structured,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except UpstreamError as exc:
        raise _chat_502(exc) from exc

    return JSONResponse(
        content={
            "request_id": out.request_id,
            "model": out.model,
            "tier": out.tier,
            "upstream_mode": out.mode,
            "structured": out.structured,
            "content": out.content,
            "usage": out.usage,
            "finish_reason": out.finish_reason,
            "attempts": out.attempts,
            "retries": out.retries,
            "latency_ms": round(out.latency_ms, 1),
            "parse_failed": out.parse_failed,
            # W2 D10：是否走了 fallback 降级（primary 重试耗尽后切备成功）
            "degraded": out.degraded,
            "degraded_from": out.degraded_from,
        },
        headers=_attempt_headers(out.attempts, out.retries, out.degraded, out.degraded_from),
    )


def _sse_body(stream: ChatStream, *, structured: bool) -> Iterator[str]:
    """把 delta 流包成 SSE。四类事件：meta（首帧，带重试统计）/ delta / done / error。

    ⚠️ 流中途的故障只发 error 事件，不悄悄重试 —— 重试只覆盖首 token 之前，
    理由见 app/llm.py 文件头取舍 2。
    """
    yield sse(
        "meta",
        {
            "request_id": stream.request_id,
            "model": stream.model,
            "tier": stream.tier,
            "upstream_mode": stream.mode,
            "attempts": stream.attempts,
            "retries": stream.retries,
            # W2 D10：首帧就告诉客户端这单是否降级
            "degraded": stream.degraded,
            "degraded_from": stream.degraded_from,
        },
    )
    parts: list[str] = []
    try:
        for delta in stream.deltas:
            parts.append(delta)
            yield sse("delta", {"delta": delta})
    except UpstreamError as exc:
        yield sse("error", {"message": exc.message, "upstream": exc.upstream, "status": exc.status})
        return
    yield sse("done", _done_payload(stream, parts, structured))


def _done_payload(stream: ChatStream, parts: list[str], structured: bool) -> dict[str, Any]:
    """流收尾帧。结构化模式下把拼回的全文字符串再 parse 一次 —— 客户端不该自己拼 JSON。"""
    content = "".join(parts)
    payload: dict[str, Any] = {
        "usage": stream.usage,
        "attempts": stream.attempts,
        "retries": stream.retries,
        "finish_reason": stream.finish_reason,
        "degraded": stream.degraded,
        "degraded_from": stream.degraded_from,
    }
    if structured:
        payload["structured"] = True
        try:
            payload["content"] = json.loads(content)
        except (TypeError, ValueError):
            # 模型没守 JSON 约定：保留原文并标 parse_failed，让调用方看到现场
            payload["content"] = content
            payload["parse_failed"] = True
    else:
        payload["content"] = content
    return payload


@app.get("/", tags=["meta"])
def root() -> dict:
    """给浏览者的门面信息，顺带自证「容器内跑的是非 root 用户」。"""
    return {
        "service": "llmops-engineer-api",
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "user": os.getenv("USER") or os.getenv("USERNAME") or "unknown",
        "load_endpoint": "enabled" if ENABLE_LOAD_ENDPOINT else "disabled",
        "llm_mode": llm.LLM_MODE,
        "docs": "/docs",
    }
