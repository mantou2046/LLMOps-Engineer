"""W7 D3 · 成本控制三件套：精确缓存 / 分流计价 / Token 审计。

⚠️ 口径边界（写在前头，避免被当成财务数字）：
  · 本模块只算「模型 Token 用量 × 演示单价」，单价是**成本点数**（cost unit），
    不是货币。沿用 W5 D3 实测的 primary : cheap = 10 : 1 关系，但没有真钱换算 ——
    网关 `model_info` 里的单价是 W5 D3 给 fake-model 的演示价，不参与业务路由。
  · **精确缓存**，不是语义缓存。W4 D5 已实测本机三条前置条件全缺
    （上游无 embedding 端点 / `redis:7` 无 RediSearch / `redisvl` 未装），
    语义缓存本机跑不起来；两条「意思相同但字面不同」的题在这里**不会**命中。
  · 向量库、rerank、缓存服务、网络出流量**都没有真实单价**，本模块只在
    `UNPRICED_ITEMS` 里显式列出口径，不编数字、不假装已计入总成本。

三件套各管一段（和 W7 阅读清单的白话概述对齐）：
  | 件 | 管什么 | 省在哪 |
  | --- | --- | --- |
  | 精确缓存 | 一模一样的单轮请求复用上次回答 | 命中那次不调模型，Token 记 0 |
  | 规则分流 | `app/routing.py` 判 primary / cheap | 简单题走 1/10 单价的档位 |
  | Token 审计 | 逐次记 tier / 用量 / 命中 / 成本点数 | 能按天、按档位出周报并复核 |

默认全部关闭：`COST_CACHE_ENABLED=0`、`COST_LEDGER_ENABLED=0`。
开关关掉时本模块对 `/chat` 零影响 —— 和 W7 D1/D2 护栏的接线纪律一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ---- 演示单价（成本点数 / 千 Token；不是钱）---------------------------------
# 比例沿用 W5 D3 实测 cheap 2e-5 vs primary 2e-4（正好 10 倍）。
# 绝对值只为了让「下降 X%」可计算，换成任何同比例单价，百分比不变。
COST_UNITS_PER_1K: dict[str, dict[str, float]] = {
    "primary": {"input": 10.0, "output": 10.0},
    "cheap": {"input": 1.0, "output": 1.0},
}

# 没有真实单价、因此**未计入**的成本项。任务卡点名「别只算模型费」，
# 这里给的是清单与需要的数据，不是假数字。
UNPRICED_ITEMS: tuple[dict[str, str], ...] = (
    {"item": "向量库（pgvector）", "needs": "实例规格 × 时长 + 存储容量", "status": "未计入"},
    {"item": "rerank（bge-reranker-base）", "needs": "GPU/CPU 时长 × 精排次数", "status": "未计入"},
    {"item": "缓存服务（Redis）", "needs": "实例规格 × 时长", "status": "未计入"},
    {"item": "网络出流量", "needs": "出网 GB × 单价", "status": "未计入"},
)

CACHE_TTL_SECONDS = int(os.getenv("COST_CACHE_TTL_SECONDS", "600"))  # 沿用 W4 DEC-032 的 600s


def estimate_cost(usage: dict[str, Any], tier: str) -> float:
    """按档位单价把 Token 用量折算成成本点数。缺字段按 0 处理，不猜。"""
    price = COST_UNITS_PER_1K.get(tier, COST_UNITS_PER_1K["primary"])
    prompt = float(usage.get("prompt_tokens", 0) or 0)
    completion = float(usage.get("completion_tokens", 0) or 0)
    return round(
        (prompt * price["input"] + completion * price["output"]) / 1000.0, 6
    )


def cache_key(
    messages: list[dict[str, Any]],
    *,
    tier: str,
    structured: bool,
    temperature: float | None,
    max_tokens: int | None,
) -> str:
    """缓存键 = 影响回答的全部入参的规范化哈希。

    故意把 tier / temperature / max_tokens 都算进去：同一句话在 primary 与 cheap
    下不是同一个回答，把它们混在一个键里会串答案。
    """
    normalized = {
        "messages": messages,
        "tier": tier,
        "structured": structured,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cacheable(
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    temperature: float | None,
) -> tuple[bool, str]:
    """能不能缓存 + 为什么不能。返回理由是为了写进报告，不是静默跳过。

    三条守卫都是从 W4 D5 的教训推出来的：
      1. 流式不缓存 —— SSE 是逐段的，缓存整段会改变传输形态。
      2. 多轮不缓存 —— 同样的最后一句在不同历史里答案不同；语义缓存那套
         「相似度命中」在多轮/Agent 里会把旧答案错发给新问题，精确缓存也要守住。
      3. temperature > 0 不缓存 —— 采样本来就不确定，缓存会把一次随机结果固化。
    """
    if stream:
        return False, "stream"
    if len(messages) != 1:
        return False, "multi_turn"
    if temperature is not None and temperature > 0:
        return False, "sampling"
    return True, "ok"


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "hit_rate": self.hit_rate,
            "skipped": dict(self.skipped),
        }


class ExactCache:
    """进程内精确缓存（TTL 可配）。生产该用 Redis，这里为了零依赖可复现。

    刻意不引入 Redis：W4 D5 已实测本机没 RediSearch/redisvl，再起一个 redis
    只会把「语义缓存跑不了」和「精确缓存能跑」混成一件事。
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}
        self.stats = CacheStats()

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        created, value = entry
        if time.monotonic() - created > self.ttl:
            del self._data[key]
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return json.loads(json.dumps(value, ensure_ascii=False))  # 深拷贝，防调用方改脏缓存

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = (time.monotonic(), json.loads(json.dumps(value, ensure_ascii=False)))
        self.stats.stores += 1

    def clear(self) -> None:
        self._data.clear()
        self.stats = CacheStats()

    def skip(self, reason: str) -> None:
        self.stats.skipped[reason] += 1


class CostLedger:
    """Token 审计：逐次记账，支持按天 / 按档位 / 按缓存命中出周报。

    账本条目只记数量与档位，**不记请求正文**（避免把用户内容写进日志）。
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        *,
        tier: str,
        usage: dict[str, Any],
        cache_hit: bool,
        query_id: str = "",
        day: str | None = None,
    ) -> dict[str, Any]:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        entry = {
            "day": day or time.strftime("%Y-%m-%d"),
            "tier": tier,
            "query_id": query_id,
            "prompt_tokens": 0 if cache_hit else prompt,
            "completion_tokens": 0 if cache_hit else completion,
            "total_tokens": 0 if cache_hit else prompt + completion,
            "cache_hit": cache_hit,
            "cost_units": 0.0 if cache_hit else estimate_cost(usage, tier),
        }
        self.entries.append(entry)
        return entry

    def weekly_report(self) -> dict[str, Any]:
        """周报：总量、每千次、命中率、按档位分布。"""
        n = len(self.entries)
        total_units = round(sum(e["cost_units"] for e in self.entries), 6)
        total_tokens = sum(e["total_tokens"] for e in self.entries)
        hits = sum(1 for e in self.entries if e["cache_hit"])
        by_tier: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "cost_units": 0.0}
        )
        by_day: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "cost_units": 0.0}
        )
        for e in self.entries:
            for bucket, key in ((by_tier, e["tier"]), (by_day, e["day"])):
                bucket[key]["calls"] += 1
                bucket[key]["tokens"] += e["total_tokens"]
                bucket[key]["cost_units"] += e["cost_units"]
        scale = 1000.0 / n if n else 0.0
        return {
            "calls": n,
            "total_tokens": total_tokens,
            "total_cost_units": total_units,
            "per_1000_tokens": round(total_tokens * scale, 1),
            "per_1000_cost_units": round(total_units * scale, 3),
            "cache_hits": hits,
            "cache_hit_rate": round(hits / n, 4) if n else 0.0,
            "by_tier": {k: dict(v) for k, v in sorted(by_tier.items())},
            "by_day": {k: dict(v) for k, v in sorted(by_day.items())},
            "unpriced_items": [dict(x) for x in UNPRICED_ITEMS],
        }

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in self.entries)


# ---- 全局单例（/chat 用；测试可自己 new 一个，别共用）-----------------------
CACHE = ExactCache()
LEDGER = CostLedger()

COST_CACHE_ENABLED = os.getenv("COST_CACHE_ENABLED", "0") == "1"
COST_LEDGER_ENABLED = os.getenv("COST_LEDGER_ENABLED", "0") == "1"
