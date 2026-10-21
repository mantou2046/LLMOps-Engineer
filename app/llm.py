"""W2 D8 · 上游 LLM 调用层：重试 / 指数退避 / 超时 / 流式 / 结构化输出

为什么从 main.py 拆出来：main.py 管「服务骨架」（探针、演示旋钮、门面信息），
本模块管「一次 /chat 怎么送到上游模型、失败了怎么办」。两类关注点的变更频率
完全不同 —— 调重试策略不该连带读懂探针语义，反之亦然。

W2 D8 计划卡验收对照：

  · 写 /chat 端点，支持结构化输出   → chat() 的 structured 分支（response_format=json_object）
  · 重试 + 指数退避 + 超时          → tenacity Retrying + openai max_retries=0 + timeout
  · 流式响应（SSE）                → open_stream() + main.py 的 _sse_body()
  · 模拟上游 500 能自动重试         → MOCK_FAIL_N_TIMES 注入（见 _mock_guard）
  · curl 能看到流式输出             → event: meta / delta / done / error 四类 SSE 事件

四条设计取舍（都是踩过坑才定下来的，别随手改）：

  1) openai SDK 的 max_retries 设为 0 —— 重试策略只留本模块一层。
     不关的话两层叠加（SDK 退避 × 我们的退避），「3 次重试」实际变 3×3 次，
     上游一次抖动能把客户端拖死十几秒。SDK 的默认重试是给「无脑调用方」兜底的，
     而我们自己管策略，就必须把它关掉。

  2) 重试只覆盖「首 token 之前」。流式一旦开始往外吐字，半截答案没法重发 ——
     客户端看到的是「答案从头再来一遍」，比断流更难排查。所以流中断只在 SSE 里
     发 error 事件，不悄悄重试（_sse_body 的 except 分支）。

  3) mock 模式是默认。.env 里 LLM_PRIMARY_API_KEY 在本地演练阶段就是空的，
     但 /chat 的可验收性不能依赖「先去申请一个真 key」。mock 提供确定性输出 +
     可注入 500 + 可调 chunk 间隔，没 key 也能把重试和流式完整跑一遍。
     ⚠️ 上生产前必须显式切 LLM_MODE=openai —— 这也是 /ready 要报告 mode 的原因。

  4) 结构化输出走 response_format json_object，而不是 function calling：
     后者要在 messages 里塞一份 JSON Schema，prompt 计价更贵、且兼容性更差
     （部分 OpenAI 兼容后端对 tools 支持残缺）。json_object 模式只需在
     messages 里出现 "json" 一词即可触发，简单且各后端都认。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterator

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

import observability
import prompts

log = logging.getLogger("llmops.llm")

# ---------------------------------------------------------------------------
# 运行模式与档位配置（env）
# ---------------------------------------------------------------------------
# mock   本地模拟上游（默认）：不调用真实模型，仅用于本地/集群内验收
# openai 真实上游：OpenAI 兼容协议（chat.completions.create）
LLM_MODE = os.getenv("LLM_MODE", "mock").strip().lower() or "mock"

# 单次上游请求的整体超时（秒）。流式请求里，httpx 会在「建连后长时间收不到字节」时
# 自行报读超时 —— 首 token 超时 ≠ 总超时，这个边界由 SDK 内部处理，这里只管单次上限。
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))

# 重试策略：共 N 次尝试（含首发）后放弃；指数退避 1s→2s→4s…，封顶 LLM_BACKOFF_MAX，
# 每次再叠加 0–0.5s 随机抖动（防多实例同时重试打同一个上游 = 惊群）。
LLM_MAX_ATTEMPTS = max(1, int(os.getenv("LLM_MAX_ATTEMPTS", "3")))
LLM_BACKOFF_MIN = float(os.getenv("LLM_BACKOFF_MIN", "1"))
LLM_BACKOFF_MAX = float(os.getenv("LLM_BACKOFF_MAX", "8"))

# ---- mock 专用旋钮（仅 LLM_MODE=mock 时生效）--------------------------------
# 注入前 N 次上游 500：直接验收「重试后成功」。0 = 不注入（正常演示）
MOCK_FAIL_N_TIMES = max(0, int(os.getenv("MOCK_FAIL_N_TIMES", "0")))
# 首 token 延迟（ms）：让 curl -N 真的看到「先等一会、再逐段到达」的流式节奏
MOCK_FIRST_TOKEN_MS = max(0, int(os.getenv("MOCK_FIRST_TOKEN_MS", "200")))
# 相邻 chunk 间隔（ms）
MOCK_CHUNK_DELAY_MS = max(0, int(os.getenv("MOCK_CHUNK_DELAY_MS", "60")))

# 结构化输出时，若调用方没给 system 消息，自动补的指令。
# OpenAI json_object 模式要求 messages 里出现 "json" 一词才生效，这句话正好满足。
@lru_cache(maxsize=1)
def _default_system_prompt() -> str:
    """结构化输出的默认 system prompt —— **读 prompts/*.yaml（唯一真源）**。

    优先级：环境变量 `STRUCTURED_SYSTEM_PROMPT`（显式覆盖，D15 起的口子）
    → `prompts/structured_json/v1.yaml` → 空串。
    ⚠️ 2026-10-21 D16：这里原本是硬编码字符串，与 prompts/*.yaml 构成「两个真相源」，
       违反 DEC-027，已改为读文件；读不到时宁可空串，也不再留副本。
    """
    override = os.getenv("STRUCTURED_SYSTEM_PROMPT", "").strip()
    if override:
        return override
    try:
        return prompts.load_prompt_local("structured_json", version=1)["template"]
    except Exception:  # noqa: BLE001 —— 文件缺失也不能让 import 期炸掉服务
        return ""


# 兼容旧引用（值来自 yaml，不再是硬编码）
STRUCTURED_SYSTEM_PROMPT = _default_system_prompt()


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class UpstreamError(Exception):
    """上游故障的统一外衣。

    retryable 是 tenacity 的唯一判据：连接失败 / 超时 / 5xx / 429 → 重试；
    401（key 错）、400（请求不合法）、404（模型名错）→ 原样抛给调用方转 502，
    重试只是浪费额度。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = True,
        upstream: str = "upstream",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.upstream = upstream


# ---------------------------------------------------------------------------
# 模型档位：primary（主模型）/ cheap（便宜分流模型）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tier:
    """一个上游模型档位。env 命名自带档位名：LLM_<TIER>_MODEL / _API_KEY / _BASE_URL。"""

    name: str
    model: str
    api_key: str
    base_url: str


def _load_tier(name: str) -> Tier:
    upper = name.upper()
    return Tier(
        name=name,
        model=os.getenv(f"LLM_{upper}_MODEL", ""),
        api_key=os.getenv(f"LLM_{upper}_API_KEY", ""),
        base_url=os.getenv(f"LLM_{upper}_BASE_URL", ""),
    )


TIERS: dict[str, Tier] = {name: _load_tier(name) for name in ("primary", "cheap", "fallback")}


# ---- fallback 通道（W2 D10）------------------------------------------------
# 语义：primary 档位重试**耗尽**后仍失败 → 自动切到 fallback 档位再试一次，
#       成功则照常返回，但在结果里标 degraded=True（调用方/面板一眼看出这单降级了）。
# 设计取舍：
#   1) fallback 不是「第 N 档模型」，而是「主通道彻底不可用时的兜底出口」——
#      所以它走**独立的 TIERS["fallback"]**，不和 primary/cheap 的量级划分混在一起。
#   2) 只切一次、不来回跳：fallback 也失败就如实抛出原始错误，不做「再切回 primary」的循环，
#      否则上游全挂时会变成两台机器隔着退避互相甩锅，延迟翻倍还难排查。
#   3) fallback 未配置（缺 key 或 model）= 降级能力不存在，行为与加此功能前完全一致。
LLM_FALLBACK_ENABLED = os.getenv("LLM_FALLBACK_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def _fallback_ready() -> bool:
    """fallback 档位是否可用。

    - openai 模式：key 和 model 都必须配（缺 key 就打不通真实上游）。
    - mock 模式：只看 model —— mock 不发真实请求，不依赖 key；这样无 key 也能
      完整验收「主通道耗尽 → 切 fallback」这条路径（与 D8 mock 可验收的既定做法一致）。
      默认给 mock fallback 一个占位 model 名，省得本地验收还要手动设 env。
    """
    if not LLM_FALLBACK_ENABLED:
        return False
    fb = TIERS["fallback"]
    if LLM_MODE == "mock":
        return bool(fb.model or True)  # mock 下永远就绪：model 名仅用于回显
    return bool(fb.api_key and fb.model)


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------
@dataclass
class ChatOutcome:
    """非流式 /chat 的完整结果（含重试统计，供响应体与响应头回显）。"""

    request_id: str
    content: str | dict
    model: str
    tier: str
    mode: str
    usage: dict
    attempts: int
    retries: int
    latency_ms: float
    structured: bool
    parse_failed: bool = False
    finish_reason: str | None = None
    # W2 D10：本次是否走了 fallback（primary 重试耗尽后切备成功）。True = 降级返回。
    degraded: bool = False
    # 降级前的原始档位名（未降级时为 None），便于排查「这单本该走谁」。
    degraded_from: str | None = None

    # W3 D16：本次请求用的 prompt 版本与内容指纹（供响应头 / trace 回显）
    prompt_version: str = ""
    prompt_hash: str = ""


@dataclass
class ChatStream:
    """流式 /chat 的增量源。

    usage 是可变字典：openai 流末 chunk（stream_options.include_usage）到达时填充；
    mock 一开始就是填好的（反正没有真实计费）。
    """

    request_id: str
    model: str
    tier: str
    mode: str
    deltas: Iterator[str]
    attempts: int
    retries: int
    structured: bool
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = "stop"
    # W2 D10：是否走了 fallback 降级（同 ChatOutcome）。
    degraded: bool = False
    degraded_from: str | None = None


# ---------------------------------------------------------------------------
# 配置体检
# ---------------------------------------------------------------------------
def chat_configured() -> bool:
    """当前模式能不能真的发出一次请求（只查配置，不发请求）。

    /ready 只报告结果、不据此摘流量：「配置没填对」该由告警发现，
    不是让 Pod 退出 Endpoints 把故障放大。
    """
    if LLM_MODE == "mock":
        return True
    primary = TIERS["primary"]
    return bool(primary.api_key and primary.model)


def _require_configured(tier: Tier) -> None:
    """openai 模式下缺 key / 缺模型名 = 不可重试的配置错误，快速失败。"""
    if LLM_MODE != "openai":
        return
    if not tier.api_key:
        raise UpstreamError(
            f"LLM_MODE=openai 但 {tier.name} 档位缺 API key（LLM_{tier.name.upper()}_API_KEY 未设）",
            status=500,
            retryable=False,
        )
    if not tier.model:
        raise UpstreamError(
            f"LLM_MODE=openai 但 {tier.name} 档位缺模型名（LLM_{tier.name.upper()}_MODEL 未设）",
            status=500,
            retryable=False,
        )


# ---------------------------------------------------------------------------
# 重试策略（tenacity 一层，SDK 层 max_retries=0）
# ---------------------------------------------------------------------------
def _log_retry_wait(retry_state: Any) -> None:
    """退避前留一行日志：失败原因 + 等多久 + 第几次重试，全在排障时用得上。"""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    next_action = getattr(retry_state, "next_action", None)
    wait_s = getattr(next_action, "sleep", 0) or 0
    log.warning(
        "上游 [%s] 调用失败（第 %d 次），%.1fs 后重试：%s",
        getattr(exc, "upstream", "upstream"),
        retry_state.attempt_number,
        wait_s,
        exc,
    )


def _retryer() -> Retrying:
    """每次调用都建一个新的 Retrying —— tenacity 的统计挂在实例上，
    共用一个实例会让并发请求互相污染 attempt_number。"""
    return Retrying(
        stop=stop_after_attempt(LLM_MAX_ATTEMPTS),
        # 指数退避 1s→2s→4s…，封顶 LLM_BACKOFF_MAX；再叠 0–0.5s 抖动防惊群
        wait=wait_exponential(multiplier=LLM_BACKOFF_MIN, min=LLM_BACKOFF_MIN, max=LLM_BACKOFF_MAX)
        + wait_random(0, 0.5),
        # 只重试「可重试的上游故障」——UpstreamError 之外的异常（我们的 bug）直接炸出去
        retry=retry_if_exception(lambda exc: isinstance(exc, UpstreamError) and exc.retryable),
        reraise=True,  # 耗尽后抛原始 UpstreamError，而不是 tenacity 的 RetryError 包装
        before_sleep=_log_retry_wait,
    )


def _attempts_of(retryer: Retrying) -> int:
    """从完成的 Retrying 实例里取「实际尝试了几次」。"""
    return int(retryer.statistics.get("attempt_number", 1))


# ---------------------------------------------------------------------------
# mock 后端（默认模式）：确定性输出 + 故障注入 + 流式节奏
# ---------------------------------------------------------------------------
_mock_calls = 0


def _mock_guard() -> None:
    """mock 故障注入：前 MOCK_FAIL_N_TIMES 次调用一律 500，之后恢复。

    这是 W2 D8 验收项「模拟上游 500 时能自动重试」的开关。
    例：MOCK_FAIL_N_TIMES=2 + LLM_MAX_ATTEMPTS=3
      → 第 1、2 次 500（各退避 1s / 2s），第 3 次成功
      → 响应体 attempts=3、retries=2，响应头 X-Upstream-Retries: 2
    """
    global _mock_calls
    _mock_calls += 1
    if _mock_calls <= MOCK_FAIL_N_TIMES:
        raise UpstreamError(
            f"mock 注入的上游故障：第 {_mock_calls}/{MOCK_FAIL_N_TIMES} 次调用返回 500",
            status=500,
            retryable=True,
            upstream="mock",
        )


def _mock_text(messages: list[dict], structured: bool) -> str:
    """mock 的回答文本。刻意把「这是 mock」写进正文 —— 演示时没人该误以为模型真的懂。"""
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    user_text = str(last_user.get("content", ""))
    if structured:
        return json.dumps(
            {
                "answer": "mock 结构化回答：这是 JSON mode 的演示输出。真实模式下这里会是模型按 schema 产出的对象。",
                "user_message_chars": len(user_text),
                "citations": [],
                "confidence": 0.42,
            },
            ensure_ascii=False,
        )
    return (
        f"这是 mock 上游的演示回答（LLM_MODE=mock，未调用真实模型）。"
        f"你这条消息 {len(user_text)} 字；请求已按真实代码路径走完 —— 重试 / 超时 / 流式都是真逻辑，"
        f"只有「模型生成」这一环是假的。切 LLM_MODE=openai 后此处即真实模型输出。"
    )


def _mock_usage(messages: list[dict], completion_text: str) -> dict:
    """mock 的 token 用量是估算值（约 3 字符/token 的中文经验值），estimated=True 供识别。"""
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    prompt_tokens = max(1, prompt_chars // 3)
    completion_tokens = max(1, len(completion_text) // 3)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }


def _mock_chat_once(tier: Tier, messages: list[dict], structured: bool) -> tuple[str, dict, str | None]:
    _mock_guard()
    text = _mock_text(messages, structured)
    return text, _mock_usage(messages, text), "stop"


def _mock_stream_start(messages: list[dict], structured: bool) -> tuple[Iterator[str], dict]:
    """mock 流：首 token 前有 MOCK_FIRST_TOKEN_MS 延迟，之后按 MOCK_CHUNK_DELAY_MS 逐段吐。"""
    text = _mock_text(messages, structured)
    usage = _mock_usage(messages, text)

    def _gen() -> Iterator[str]:
        time.sleep(MOCK_FIRST_TOKEN_MS / 1000.0)
        for i in range(0, len(text), 12):
            time.sleep(MOCK_CHUNK_DELAY_MS / 1000.0)
            yield text[i : i + 12]

    return _gen(), usage


# ---------------------------------------------------------------------------
# openai 后端：OpenAI 兼容协议（chat.completions.create）
# ---------------------------------------------------------------------------
_clients: dict[str, Any] = {}


def _openai_client(tier: Tier) -> Any:
    """按档位缓存 client —— 连接池是复用的，每次新建等于每次重做 TCP+TLS 握手。"""
    client = _clients.get(tier.name)
    if client is not None:
        return client
    from openai import OpenAI  # 延迟导入：mock 模式（默认）不该为它付 import 时间

    client = OpenAI(
        api_key=tier.api_key or "sk-not-configured",
        base_url=tier.base_url or None,  # None → 官方默认端点；填了就是自建/网关
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=0,  # 文件头取舍 1：重试只留 tenacity 一层
    )
    _clients[tier.name] = client
    return client


def _wrap_openai_error(exc: Exception) -> UpstreamError:
    """把 openai SDK 的异常翻译成 UpstreamError，并判定该不该重试。

    判据：连接失败 / 读超时 / 408 / 409 / 429 / 5xx → 重试；
    400 / 401 / 403 / 404 → 不重试（请求或凭据有问题，重来一遍结果一样）。
    """
    retryable = False
    status = getattr(exc, "status_code", None)
    try:
        from openai import APIConnectionError, APITimeoutError

        retryable = isinstance(exc, (APIConnectionError, APITimeoutError))
    except ImportError:  # pragma: no cover - 理论上到不了
        pass
    if not retryable and isinstance(status, int):
        retryable = status >= 500 or status in (408, 409, 429)
    return UpstreamError(
        f"{type(exc).__name__}: {exc}",
        status=status,
        retryable=retryable,
        upstream="openai",
    )


def _openai_chat_once(
    tier: Tier,
    messages: list[dict],
    structured: bool,
    temperature: float | None,
    max_tokens: int | None,
) -> tuple[str, dict, str | None]:
    client = _openai_client(tier)
    kwargs: dict[str, Any] = {
        "model": tier.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if structured:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - openai 异常体系太宽，进来统一翻译
        raise _wrap_openai_error(exc) from exc
    choice = resp.choices[0]
    usage_obj = getattr(resp, "usage", None)
    usage = {
        "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
        "completion_tokens": getattr(usage_obj, "completion_tokens", None),
        "total_tokens": getattr(usage_obj, "total_tokens", None),
        "estimated": False,
    }
    return choice.message.content or "", usage, choice.finish_reason


def _openai_stream_start(
    tier: Tier,
    messages: list[dict],
    structured: bool,
    temperature: float | None,
    max_tokens: int | None,
) -> tuple[Iterator[str], dict]:
    """发起 openai 流式请求，返回 (delta 迭代器, usage 袋)。

    usage 袋在流末 chunk（include_usage）到达时被填充；_sse_body 的 done 事件读它。
    """
    client = _openai_client(tier)
    kwargs: dict[str, Any] = {
        "model": tier.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if structured:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        wrapped = _wrap_openai_error(exc)
        # 部分 OpenAI 兼容后端不认 stream_options —— 剥掉它再试一次，
        # 不然「流式 + 该后端」这条路整个走不通
        if "stream_options" in kwargs and wrapped.status == 400:
            kwargs.pop("stream_options")
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as exc2:  # noqa: BLE001
                raise _wrap_openai_error(exc2) from exc2
        else:
            raise wrapped from exc

    usage_sink: dict = {}

    def _gen() -> Iterator[str]:
        try:
            for chunk in resp:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage_sink.update(
                        {
                            "prompt_tokens": getattr(chunk_usage, "prompt_tokens", None),
                            "completion_tokens": getattr(chunk_usage, "completion_tokens", None),
                            "total_tokens": getattr(chunk_usage, "total_tokens", None),
                            "estimated": False,
                        }
                    )
                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None)
                    if text:
                        yield text
        finally:
            _safe_close(resp)

    return _gen(), usage_sink


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def _safe_close(obj: Any) -> None:
    """安静地关掉一个上游流（openai Stream 持有 HTTP 连接）。
    关闭动作失败不值得让主流程炸掉 —— 最坏情况连接被 GC 时回收。"""
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            log.debug("关闭上游流时忽略异常", exc_info=True)


def _chain(first: str, rest: Iterator[str]) -> Iterator[str]:
    """把「重试循环里已逼出来的首 chunk」接回剩余流 —— 首 token 不能吞。"""
    yield first
    yield from rest


def sse(event: str, data: dict) -> str:
    """一个 SSE 帧。data 一律 JSON（ensure_ascii=False：中文不转义，
    客户端少一层 decode，也避免老客户端把 \\uXXXX 显示成字面量）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _prepare(
    messages: list[dict], structured: bool, system_prompt: str | None = None
) -> list[dict]:
    """拷贝一份 messages，并按需补结构化 system 指令（不修改调用方传入的列表）。

    W3 D16：system_prompt 由 app/prompts.py 按 registry 的 label 解析出来；
    取不到时（回落失败 / 未接入）仍用模块常量兜底，保证行为与接入前一致。
    """
    payload = [dict(m) for m in messages]
    if structured and not any(m.get("role") == "system" for m in payload):
        payload.insert(0, {"role": "system", "content": system_prompt or STRUCTURED_SYSTEM_PROMPT})
    return payload


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def chat(
    *,
    messages: list[dict],
    tier_name: str = "primary",
    structured: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = 1024,
) -> ChatOutcome:
    """非流式对话。structured=True 且解析成功时 content 是 dict，否则原文 str。

    W2 D10：传 tier_name="primary" 且 fallback 已配置时，primary 重试耗尽自动切 fallback
    （结果 degraded=True）；tier_name 显式指定为其他档位（如 "cheap"）不触发降级。
    """
    t0 = time.perf_counter()
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    tier = TIERS[tier_name]
    _require_configured(tier)
    # W3 D16：prompt 每请求解析（SDK 侧 60s 缓存）—— 「改 label 下个请求就生效」的前提
    meta = prompts.load_prompt("structured_json") if structured else None
    payload = _prepare(messages, structured, meta["template"] if meta else None)

    def _once_for(t: Tier) -> tuple[str, dict, str | None]:
        if LLM_MODE == "openai":
            return _openai_chat_once(t, payload, structured, temperature, max_tokens)
        return _mock_chat_once(t, payload, structured)

    def _run_retry(t: Tier) -> tuple[str, dict, str | None, int]:
        """对某个档位跑完整重试循环，返回 (content, usage, finish, attempts)。"""
        retryer = _retryer()
        for attempt in retryer:
            with attempt:
                content, usage, finish = _once_for(t)
        return content, usage, finish, _attempts_of(retryer)

    # trace 包在重试循环**外面**：一次 /chat 一条 trace，attempts/retries 记在 trace 里，
    # 而不是每次重试各起一条 —— 否则面板上「3 次重试」看起来像「3 次独立请求」。
    with observability.trace_chat(
        request_id=request_id,
        model=tier.model or f"mock-{tier.name}",
        tier=tier.name,
        mode=LLM_MODE,
        messages=payload,
        structured=structured,
        stream=False,
        prompt_meta=meta,
    ) as rec:
        degraded = False
        degraded_from: str | None = None
        # ---- 主通道：按请求的档位跑 ----
        try:
            content, usage, finish, attempts = _run_retry(tier)
        except UpstreamError as primary_exc:
            # 主通道重试耗尽仍失败。若这是默认的 primary 且 fallback 就绪 → 降级重试。
            if tier_name == "primary" and _fallback_ready():
                fb = TIERS["fallback"]
                log.warning(
                    "主通道 [%s] 重试耗尽（%s），降级切 fallback [%s]：%s",
                    tier.name,
                    primary_exc.upstream,
                    fb.model,
                    primary_exc,
                )
                content, usage, finish, attempts = _run_retry(fb)
                degraded = True
                degraded_from = tier.name
                tier = fb
                rec["tier"] = fb.name  # 面板上这条 trace 归属实际服务的档位
            else:
                # 没配 fallback（或显式指定了非 primary）→ 如实抛出，行为与加此功能前一致
                raise

        parse_failed = False
        if structured and isinstance(content, str):
            try:
                content = json.loads(content)
            except (TypeError, ValueError):
                # 模型没守 JSON 约定：保留原文 + 标记，让调用方看到现场而不是拿到 null
                parse_failed = True

        rec["usage"] = usage
        rec["content"] = content
        rec["finish_reason"] = finish
        if degraded:
            rec["degraded"] = True
            rec["degraded_from"] = degraded_from

    return ChatOutcome(
        request_id=request_id,
        content=content,
        model=tier.model or f"mock-{tier.name}",
        tier=tier.name,
        mode=LLM_MODE,
        usage=usage,
        attempts=attempts,
        retries=max(0, attempts - 1),
        latency_ms=(time.perf_counter() - t0) * 1000,
        structured=structured,
        parse_failed=parse_failed,
        finish_reason=finish,
        degraded=degraded,
        degraded_from=degraded_from,
        prompt_version=(f"structured_json@v{meta['version']}" if meta else ""),
        prompt_hash=(meta["hash"] if meta else ""),
    )


def open_stream(
    *,
    messages: list[dict],
    tier_name: str = "primary",
    structured: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = 1024,
) -> ChatStream:
    """流式对话。重试只覆盖首 token 之前（文件头取舍 2）。

    返回的 ChatStream.deltas 已含首 chunk —— 它是在重试循环里逼出来的，
    逼出来之后不能再吞掉，否则首 token 白等一次退避。

    W2 D10：tier_name="primary" 且 fallback 已配置时，primary 重试耗尽（首 token 逼不出来）
    自动切 fallback（结果 degraded=True）。首 token 一旦已吐出则不降级 —— 半截答案不能重发。
    """
    t0 = time.perf_counter()
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    tier = TIERS[tier_name]
    _require_configured(tier)
    payload = _prepare(messages, structured)

    # 上一次尝试半途而废的流要关掉（openai Stream 持有 HTTP 连接）——
    # 否则每次重试漏一条连接。关闭时机选在下一次 _once() 进门时：
    # 退避等待期间旧连接已断开，不和 sleep 重叠占用。
    stale: dict[str, Any] = {"gen": None}

    def _once_for(t: Tier) -> tuple[Iterator[str], dict, str]:
        _safe_close(stale.get("gen"))
        stale["gen"] = None
        if LLM_MODE == "openai":
            gen, usage_sink = _openai_stream_start(t, payload, structured, temperature, max_tokens)
        else:
            _mock_guard()
            gen, usage_sink = _mock_stream_start(payload, structured)
        stale["gen"] = gen
        first = next(gen)  # 逼出首 token：连接失败 / 上游 5xx / 读超时都在这里暴露成可重试错误
        return gen, usage_sink, first

    def _run_retry(t: Tier) -> tuple[Iterator[str], dict, str, int]:
        retryer = _retryer()
        for attempt in retryer:
            with attempt:
                gen, usage_sink, first = _once_for(t)
        return gen, usage_sink, first, _attempts_of(retryer)

    degraded = False
    degraded_from: str | None = None
    try:
        gen, usage_sink, first, attempts = _run_retry(tier)
    except UpstreamError as primary_exc:
        # 首 token 都逼不出来 = 上游彻底不可用。此时切 fallback 是安全的：
        # 还没往客户端吐过任何字节，不存在「半截答案重发」问题。
        if tier_name == "primary" and _fallback_ready():
            fb = TIERS["fallback"]
            log.warning(
                "流式主通道 [%s] 首 token 逼不出（%s），降级切 fallback [%s]：%s",
                tier.name,
                primary_exc.upstream,
                fb.model,
                primary_exc,
            )
            gen, usage_sink, first, attempts = _run_retry(fb)
            degraded = True
            degraded_from = tier.name
            tier = fb
        else:
            raise

    # ⚠️ 流式的 trace 不能在**这里**结束：open_stream() 一返回，调用方还没消费完，
    #    usage_sink 此刻是空的（openai 要到流末才填）。
    #    所以把 trace 上下文搬进生成器 —— 直到 deltas 迭代完才 end()。
    #    trace_chat 内的 with 必须包住整个 yield from，否则面板上 token 永远是 0。
    def _traced() -> Iterator[str]:
        with observability.trace_chat(
            request_id=request_id,
            model=tier.model or f"mock-{tier.name}",
            tier=tier.name,
            mode=LLM_MODE,
            messages=payload,
            structured=structured,
            stream=True,
        ) as rec:
            # ⚠️ usage 绑定的是**同一个字典对象**，不是快照：
            #    openai 流末原地更新它，SSE 的 done 帧才能读到成本。
            rec["usage"] = usage_sink
            buf: list[str] = []
            try:
                for chunk in _chain(first, gen):
                    buf.append(chunk)
                    yield chunk
            finally:
                # 流过早断开（客户端超时/取消）时也要收尾：能拿多少算多少
                rec["content"] = "".join(buf)
                _safe_close(stale.get("gen"))

    return ChatStream(
        request_id=request_id,
        model=tier.model or f"mock-{tier.name}",
        tier=tier.name,
        mode=LLM_MODE,
        deltas=_traced(),
        attempts=attempts,
        retries=max(0, attempts - 1),
        structured=structured,
        usage=usage_sink,
        degraded=degraded,
        degraded_from=degraded_from,
    )
