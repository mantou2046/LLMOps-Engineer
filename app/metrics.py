"""W5 D1 · Prometheus 指标层（HTTP 三件套 + /metrics 端点）

为什么单独一个模块，而不是塞进 main.py：
  main.py 管「接口协议」，本模块管「怎么把请求变成时序数据」。
  和 observability.py 同一个理由 —— 埋点是横切关注点，混进业务文件后
  「加一个指标」就要改一遍路由代码。

今天只上三个指标，故意不铺开（阅读清单的原话是「先跑通请求数 / 错误数 / 延迟，
别一上来铺十几个」）：
  · llmops_http_requests_total             Counter   请求总数（method / route / status）
  · llmops_http_request_duration_seconds   Histogram 请求耗时（method / route）
  · llmops_http_requests_in_flight         Gauge     正在处理中的请求数
错误数不是单独的指标 —— 它就是 requests_total 里 status>=500 的那部分。
单独再做一个 counter 会让「总数」和「错误数」两个口径各自漂移，迟早对不上。

三条设计约束（都是踩过才知道的）：

1. label 用**路由模板**（`/chat`），不用原始路径（`/chat?x=1`）。
   一个 label 值 = 一条时序。用原始 URL 的话，每个带不同 query 的请求都会
   新开一条时序 —— 这是 Prometheus 被撑爆最常见的死法。
   实测见 W5 D1 手册「基数实验」：1000 个不同 label 值 → 1000 条时序。

2. `/metrics` 自己**不计数**。
   Prometheus 每 15 秒来抓一次，一天 5760 次。它计进去会让「请求量」这条曲线
   永久带上一个底噪，而且这个底噪跟业务毫无关系。
   ⚠️ 同理要小心探针：/health 每 3 秒一次 = 一天 28800 次，能把真实业务量压成
   一条平线（W2 D11 在 Langfuse 里就吃过这个亏：200 条 observation 里 199 条是探针）。
   今天的处理是**留着但打得上标签**，查询时用 `route!~"/health|/ready"` 排掉，
   而不是干脆不记 —— 探针失败率本身也是要盯的指标。

3. 分桶边界是**先定死**的，不是自动的。
   Histogram 的 P95 是从桶里插值算出来的，桶挑得不合适，算出来的分位数就是假的。
   这里按「本项目真实量级」定：探针类 5–50ms、普通接口 50–500ms、
   /chat 非流式几秒、流式几十秒 —— 所以桶一直排到 60 秒。
   ⚠️ 桶只能改大不能改小得随意：改了桶 = 历史数据不可比，等于换了一个指标。

已知局限（今天不解决，写在这里免得以后当成 bug 查）：
  · `@app.middleware("http")` 走的是 Starlette 的 BaseHTTPMiddleware，
    它量的是「到响应头就绪为止」。对**流式**响应，这只等于首字节时间，
    整个生成过程不计入 —— 所以 /chat?stream=true 在直方图里看着只有几毫秒。
    这不是 bug，是「HTTP 层指标」和「LLM 层指标」的分界：
    TTFT / 端到端生成耗时属于 W5 D3，量在网关层（LiteLLM 自带
    `litellm_llm_api_time_to_first_token_metric`）。
  · 多 worker 会各算各的（见文件末尾 PROMETHEUS_MULTIPROC_DIR 说明）。
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# 三个指标
# ---------------------------------------------------------------------------
# ⚠️ 名字前缀统一 llmops_：Prometheus 里指标名是全局命名空间，
#    不加前缀迟早跟 exporter / 库自带的 process_*、python_* 混在一起分不清归属。
#
# ⚠️ HELP 描述一律用**英文**（2026-10-30 实测）：
#    /metrics 会被各种客户端读取，而本机控制台默认代码页是 936(GBK)，Python 却按
#    UTF-8 输出 → 中文 HELP 在 PowerShell 里显示成乱码（"请求总数" → "璇锋眰鎬绘暟"）。
#    指标说明这类「给人看的元数据」写在本文件的注释里；**端点输出保持纯 ASCII**，
#    任何客户端、任何代码页下都不会乱码。
_REQUESTS = Counter(
    "llmops_http_requests_total",
    "Total HTTP requests",
    ["method", "route", "status"],
)

# 桶的选法见文件头约束 3。13 个边界 = 14 个桶（含 +Inf），
# 一条时序的成本 ≈ 桶数，所以别随手铺 30 个桶。
_DURATION = Histogram(
    "llmops_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

_IN_FLIGHT = Gauge(
    "llmops_http_requests_in_flight",
    "HTTP requests currently in flight",
)

# 不参与统计的路径。只用精确匹配：/metrics 是唯一一个「被机器高频访问、
# 但访问量本身没有业务含义」的端点。
_EXCLUDED_PATHS = frozenset({"/metrics"})

# 没匹配到路由时用的占位名（404 / 405 都归到这一条）。
# 用固定字符串而不是原始路径 —— 否则扫描器打一堆随机路径就能把时序数打爆。
_UNMATCHED = "__unmatched__"


def _route_of(request: Request) -> str:
    """取路由模板。

    ⚠️ 必须在 `call_next()` **之后**取：`scope["route"]` 是路由匹配时才写进去的，
    middleware 进入时它还不存在（那时只有 `scope["path"]`，是原始路径）。
    这是这套埋点最容易写错的一行 —— 写成 middleware 入口取，全表都变成 __unmatched__。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or _UNMATCHED


def install(app: FastAPI) -> None:
    """把埋点中间件和 /metrics 端点挂到 app 上。main.py 只需调一次。"""

    @app.middleware("http")
    async def _prometheus_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)

        _IN_FLIGHT.inc()
        status = 500  # 兜底：请求抛异常没走到 response 时，按 500 记，别让它悄悄消失
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - t0
            _IN_FLIGHT.dec()
            route = _route_of(request)
            _REQUESTS.labels(request.method, route, str(status)).inc()
            _DURATION.labels(request.method, route).observe(elapsed)

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics() -> Response:
        """Prometheus 的抓取入口。

        `include_in_schema=False`：这是给机器看的，别出现在 /docs 里误导人。
        media_type 必须用 CONTENT_TYPE_LATEST（带 `; version=0.0.4`）——
        写死 `text/plain` 也能抓，但 Prometheus 会按最老的格式解析。
        """
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
