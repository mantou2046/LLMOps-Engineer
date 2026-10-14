"""W2 D9 · 观测层：把每次 LLM 调用打成 Langfuse trace

为什么单独一个模块，而不是塞进 llm.py：
  llm.py 管「怎么把请求送到上游、失败了怎么办」；本模块管「把这次调用记下来给别人看」。
  观测是**横切关注点** —— 它不该侵入调用逻辑，否则「加一个观测后端」就要改遍调用代码。
  这里用最轻的耦合方式：llm.py 只在关键节点发事件，本模块负责把事件翻译成 Langfuse 语义。

三档开关（OBSERVABILITY）：
  off     完全不观测（默认）—— 连 SDK 都不导入，零开销
  langfuse  打到自托管 Langfuse（需要 LANGFUSE_* 三件套）
  log     只写本地日志（调试用，不需要任何服务在跑）

⚠️ 为什么默认 off，而不是「配了 key 就自动开」：
   观测组件和业务服务的**可靠性等级不同**。Langfuse 挂了不该让 /chat 挂 ——
   所以启用必须是**显式**的（OBSERVABILITY=langfuse），而不是「碰巧 .env 里有 key」。
   这跟 D5 那两个演示旋钮同一个原则：演示/附加能力默认关闭，显式开启。

⚠️ 上报是**异步且失败无害**的：
   Langfuse SDK 自己把事件排队、批量发送、失败重试。所以就算 Langfuse 中途挂了，
   /chat 也照常返回 —— 最坏情况是「这段时间的 trace 丢了」，而不是「服务不可用」。
   这是观测系统的正确设计目标：**永远不拖垮被观测的业务**。

成本估算（本模块的 cost 字段）：
  Langfuse 面板要显示「这次调用花了多少钱」，需要 token 数 × 单价。
  单价按模型名从 _PRICING 表查（单位：美元 / 100 万 token）。
  ⚠️ 表里的价格是**写死的参考值**，会过时 —— 真实项目应从配置或模型目录读。
  本地 mock 模式的 usage 是估算的（estimated=True），成本自然也是估算的，面板上要能分辨。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("llmops.observability")

# ---------------------------------------------------------------------------
# 开关与配置
# ---------------------------------------------------------------------------
# off / langfuse / log
OBSERVABILITY = os.getenv("OBSERVABILITY", "off").strip().lower() or "off"

LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# 采样率：1.0 = 全量。本地磁盘有限，D11 压测时要调低（见 D11 卡点）。
# ⚠️ 采样只影响「上报与否」，不影响业务逻辑。
try:
    LANGFUSE_SAMPLE_RATE = float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))
except ValueError:
    LANGFUSE_SAMPLE_RATE = 1.0

# ---------------------------------------------------------------------------
# 价格表（美元 / 100 万 token）—— 单位价格会变，这里只是本地估算的参考值
# ---------------------------------------------------------------------------
# 格式：模型名（小写，做子串匹配）→ (输入单价, 输出单价)
_PRICING: dict[str, tuple[float, float]] = {
    # freellmapi 网关的 auto 路由（转发到后端某模型）—— 无公开牌价，按中档模型估
    "auto": (0.30, 1.20),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen-plus": (0.40, 1.20),
    "qwen-turbo": (0.05, 0.20),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
}


def estimate_cost(model: str, usage: dict | None) -> dict:
    """按 model + usage 估算成本。模型不在表里时返回 cost=None（面板显示「未知」而不是 0）——

    ⚠️ 未知价格**不能**当 0 上报：那会让「成本统计」少算，比缺失更危险。
       None 在 Langfuse 里显示为「没有成本数据」，语义正确。
    """
    if not usage:
        return {"input": None, "output": None, "total": None, "currency": "USD"}
    m = (model or "").lower()
    price = None
    for key, val in _PRICING.items():
        if key in m:
            price = val
            break
    if price is None:
        return {"input": None, "output": None, "total": None, "currency": "USD"}
    pin, pout = price
    it = usage.get("prompt_tokens") or 0
    ot = usage.get("completion_tokens") or 0
    cost_in = it / 1_000_000 * pin
    cost_out = ot / 1_000_000 * pout
    return {
        "input": round(cost_in, 8),
        "output": round(cost_out, 8),
        "total": round(cost_in + cost_out, 8),
        "currency": "USD",
    }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
_client: Any = None
_client_ready = False


def _get_client() -> Any:
    """惰性拿 Langfuse 客户端。返回 None 表示「不可用，跳过观测」。"""
    global _client, _client_ready
    if _client_ready:
        return _client
    _client_ready = True
    if OBSERVABILITY != "langfuse":
        return None
    if not (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        log.warning(
            "OBSERVABILITY=langfuse 但缺 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY —— 观测关闭",
        )
        return None
    try:
        from langfuse import Langfuse  # 延迟导入：off 模式不付 import 开销

        _client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
            sample_rate=LANGFUSE_SAMPLE_RATE,
        )
        log.info("Langfuse 观测已启用：host=%s sample_rate=%.2f", LANGFUSE_HOST, LANGFUSE_SAMPLE_RATE)
    except Exception:  # noqa: BLE001 - 观测组件初始化失败绝不能拖垮服务
        log.warning("Langfuse 客户端初始化失败 —— 观测关闭", exc_info=True)
        _client = None
    return _client


def enabled() -> bool:
    """是否真的在观测（供 /ready 报告）。"""
    return OBSERVABILITY != "off" and _get_client() is not None


def status() -> dict:
    """观测状态快照，给探针/门面用。"""
    return {
        "mode": OBSERVABILITY,
        "enabled": enabled(),
        "host": LANGFUSE_HOST if OBSERVABILITY == "langfuse" else None,
        "sample_rate": LANGFUSE_SAMPLE_RATE if OBSERVABILITY == "langfuse" else None,
    }


# ---------------------------------------------------------------------------
# trace 封装
# ---------------------------------------------------------------------------
@contextmanager
def trace_chat(
    *,
    request_id: str,
    model: str,
    tier: str,
    mode: str,
    messages: list[dict],
    structured: bool,
    stream: bool,
) -> Iterator[dict]:
    """一次 /chat 的 trace 上下文。

    用法（在 llm.py 里）：
        with trace_chat(...) as rec:
            ...
            rec["usage"] = usage        # 回填 token
            rec["content"] = text       # 回填输出

    每个字段都是「尽力而为」：任何一步失败都只影响观测，不影响请求本身。
    设计上 with 块**不吞异常** —— 业务异常照常向上抛，只是顺带把错误记进 trace。
    """
    rec: dict[str, Any] = {
        "usage": None,
        "content": None,
        "finish_reason": None,
        "error": None,
        "_t0": time.perf_counter(),
    }
    # ⚠️ 客户端必须在这里拿：OBSERVABILITY=off 时 _get_client() 返回 None（且不导入 SDK），
    #    下面的分支据此决定「完全不观测」。惰性获取在 _get_client 里做缓存。
    client = _get_client()
    if OBSERVABILITY == "log":
        try:
            yield rec
        except BaseException as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
            _log_only(request_id, model, tier, messages, rec, stream=stream, structured=structured)
            raise
        _log_only(request_id, model, tier, messages, rec, stream=stream, structured=structured)
        return
    if client is None:
        # 既非 langfuse 也非 log（即 off）：仍然 yield，让调用方代码无需分支
        yield rec
        return

    try:
        span_cm, gen_cm = _langfuse_trace(
            client, request_id, model, tier, mode, messages, structured, stream
        )
        with span_cm:
            with gen_cm as generation:
                try:
                    yield rec
                except BaseException as exc:  # noqa: BLE001
                    # 业务异常：记进 trace 后**照常上抛** —— 观测不改变请求语义
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    _finalize_langfuse(generation, model, rec)
                    raise
                _finalize_langfuse(generation, model, rec)
    except Exception:  # noqa: BLE001
        # 观测自身出错：记录但不上抛 —— 业务请求该怎么走还怎么走。
        # ⚠️ 这里必须用 except Exception（而非 BaseException）：
        #    上面 re-raise 的业务异常若被吞掉，观测就从「旁路」变成了「改变语义」。
        log.debug("Langfuse trace 上报失败", exc_info=True)


def _langfuse_trace(client, request_id, model, tier, mode, messages, structured, stream):
    """构造 Langfuse 的 trace + generation 上下文（**SDK v3 API**）。

    层级设计：
      · 外层 span  = 「一次 HTTP 请求」（trace 根）
      · 内层 generation = 「一次模型调用」（能带 model / usage / cost）
    分开的理由：将来一次请求可能有多次模型调用（如 RAG 先改写问题再回答），
    那时一个 trace 下有多个 generation —— 现在就把层级留出来。

    ⚠️ 版本差异（实测 3.15.0）：
       SDK v2 的 `client.trace(...).generation(...)` 在 v3 已**移除**（无 `trace` 方法）。
       v3 改用 OTel 风格的 `start_as_current_span` / `start_as_current_generation`，
       靠 contextvar 自动嵌套 —— 内层自动挂到外层，不用手动传 parent。
    """
    span_cm = client.start_as_current_span(
        name="chat",
        input={"messages": messages},
        # request_id 带进 metadata：面板上能按「哪一次 HTTP 请求」反查 trace，
        # 没有它就只能在海量 trace 里按时间戳猜。
        metadata={
            "request_id": request_id,
            "tier": tier,
            "upstream_mode": mode,
            "structured": structured,
            "stream": stream,
        },
    )
    gen_cm = client.start_as_current_generation(
        name=f"chat.{tier}",
        model=model,
        input=messages,
        metadata={"request_id": request_id, "stream": stream, "structured": structured},
    )
    return span_cm, gen_cm


def _finalize_langfuse(generation, model: str, rec: dict) -> None:
    """把回填的 usage / content / error 写进 generation。

    ⚠️ v3 里 `end()` **不接受** output/usage_details 这些字段了 ——
       要么在创建时传，要么先 `update()` 再 `end()`。这里用后者（usage 要到流末才知道）。

    ⚠️ 成本的两条路径（实测踩坑）：
       · 本地能算（模型在 _PRICING 表里）→ 上报 cost_details，面板直接用我们的估算值。
       · 本地算不出（模型不在表里）→ **完全不传 cost_details**（连空 map 都不传），
         把定价权交给 Langfuse 服务端的 Models 目录（Project Settings → Models）。
         若传了空 `{}`，Langfuse 会认为「成本已提供且为 0」，从而**跳过目录匹配** ——
         这正是「面板 cost 恒为 0」的根因。
    """
    usage = rec.get("usage") or {}
    cost = estimate_cost(model, usage)
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    # 只有三个价都算出来才上报；否则整条 cost_details 不传，让 Langfuse 目录兜底
    kwargs: dict = {}
    if cost.get("total") is not None:
        kwargs["cost_details"] = {
            "input": cost["input"],
            "output": cost["output"],
            "total": cost["total"],
        }
    try:
        generation.update(
            output=rec.get("content"),
            usage_details={
                "input": prompt_tokens,
                "output": completion_tokens,
                "total": usage.get("total_tokens"),
            },
            level="ERROR" if rec.get("error") else "DEFAULT",
            status_message=rec.get("error") or None,
            **kwargs,
        )
    except Exception:  # noqa: BLE001
        log.debug("generation.update() 失败", exc_info=True)


def _log_only(request_id, model, tier, messages, rec, *, stream, structured):
    """OBSERVABILITY=log：只写本地日志，不需要任何服务在跑。

    用途：调试「trace 里到底该有什么字段」，或在没有 Langfuse 的环境里也能看到
    「这次调用记录了什么」。格式刻意压成一行，方便 grep。
    """
    usage = rec.get("usage") or {}
    cost = estimate_cost(model, usage)
    latency_ms = (time.perf_counter() - rec["_t0"]) * 1000
    content = rec.get("content")
    preview = (str(content)[:120] + "…") if content and len(str(content)) > 120 else content
    log.info(
        "trace id=%s model=%s tier=%s stream=%s structured=%s "
        "in_tok=%s out_tok=%s cost_usd=%s latency_ms=%.0f err=%s output=%r",
        request_id, model, tier, stream, structured,
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
        cost.get("total"), latency_ms, rec.get("error"), preview,
    )


# ---------------------------------------------------------------------------
# flush（进程退出前把队列里的 trace 发出去）
# ---------------------------------------------------------------------------
def flush() -> None:
    """把 SDK 内存队列里的事件发完。服务关闭钩子里调。

    ⚠️ 不 flush 的话，最后一批 trace 会随进程消失 ——
    「明明调了接口，面板上却查不到」十有八九是这个原因。
    """
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # noqa: BLE001
        log.debug("Langfuse flush 失败", exc_info=True)
