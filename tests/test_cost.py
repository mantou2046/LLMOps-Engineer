"""W7 D3 · 成本控制三件套单测：缓存守卫、计价、账本周报、/chat 接线。

全部离线、零真实模型调用。
"""

from __future__ import annotations

import cost
import main
import pytest
from fastapi.testclient import TestClient


def _msg(text: str = "这个字段是什么意思？") -> list[dict]:
    return [{"role": "user", "content": text}]


# ---- 计价 ------------------------------------------------------------------
def test_estimate_cost_uses_tier_price_and_10x_ratio():
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    assert cost.estimate_cost(usage, "primary") == 20.0
    assert cost.estimate_cost(usage, "cheap") == 2.0
    assert cost.estimate_cost({}, "cheap") == 0.0


def test_estimate_cost_unknown_tier_falls_back_to_primary():
    assert cost.estimate_cost({"prompt_tokens": 1000}, "fallback") == 10.0


# ---- 缓存键与守卫 ----------------------------------------------------------
def test_cache_key_changes_with_tier_and_temperature():
    base = dict(messages=_msg(), structured=False, temperature=None, max_tokens=1024)
    a = cost.cache_key(tier="primary", **base)
    b = cost.cache_key(tier="cheap", **base)
    c = cost.cache_key(tier="primary", **{**base, "temperature": 0.7})
    assert a != b and a != c


def test_cache_key_is_stable_for_same_input():
    kw = dict(messages=_msg(), tier="cheap", structured=False, temperature=None, max_tokens=1024)
    assert cost.cache_key(**kw) == cost.cache_key(**kw)


@pytest.mark.parametrize(
    ("messages", "stream", "temperature", "reason"),
    [
        (_msg(), True, None, "stream"),
        (_msg() + [{"role": "assistant", "content": "上轮回答"}], False, None, "multi_turn"),
        (_msg(), False, 0.7, "sampling"),
    ],
)
def test_cacheable_guards(messages, stream, temperature, reason):
    ok, why = cost.cacheable(messages, stream=stream, temperature=temperature)
    assert not ok and why == reason


def test_cacheable_allows_single_turn_deterministic():
    assert cost.cacheable(_msg(), stream=False, temperature=0) == (True, "ok")


# ---- 缓存行为 --------------------------------------------------------------
def test_exact_cache_hit_and_stats():
    c = cost.ExactCache(ttl_seconds=60)
    key = cost.cache_key(_msg(), tier="cheap", structured=False, temperature=None, max_tokens=1024)
    assert c.get(key) is None
    c.put(key, {"content": "答案"})
    assert c.get(key) == {"content": "答案"}
    assert c.stats.hits == 1 and c.stats.misses == 1 and c.stats.stores == 1
    assert c.stats.hit_rate == 0.5


def test_cache_does_not_alias_stored_value():
    c = cost.ExactCache()
    key = "k"
    c.put(key, {"nested": {"v": 1}})
    got = c.get(key)
    got["nested"]["v"] = 99
    assert c.get(key)["nested"]["v"] == 1


def test_cache_respects_ttl(monkeypatch):
    c = cost.ExactCache(ttl_seconds=1)
    ticks = [0.0]
    monkeypatch.setattr(cost.time, "monotonic", lambda: ticks[0])
    c.put("k", {"v": 1})
    ticks[0] = 2.0
    assert c.get("k") is None


def test_skip_reason_is_counted():
    c = cost.ExactCache()
    c.skip("multi_turn")
    c.skip("multi_turn")
    c.skip("stream")
    assert c.stats.as_dict()["skipped"] == {"multi_turn": 2, "stream": 1}


# ---- 账本 ------------------------------------------------------------------
def test_ledger_hit_records_zero_usage_and_cost():
    ledger = cost.CostLedger()
    ledger.record(tier="primary", usage={"prompt_tokens": 500, "completion_tokens": 100}, cache_hit=False)
    ledger.record(tier="primary", usage={"prompt_tokens": 500, "completion_tokens": 100}, cache_hit=True)
    report = ledger.weekly_report()
    assert report["calls"] == 2
    assert report["total_tokens"] == 600
    assert report["cache_hit_rate"] == 0.5
    assert report["by_tier"]["primary"]["calls"] == 2
    # 6.0 cost units / 2 次调用 × 1000 = 每千次 3000 点（命中那次记 0）
    assert report["per_1000_cost_units"] == pytest.approx(3000.0, abs=0.01)


def test_ledger_to_jsonl_has_no_request_text():
    ledger = cost.CostLedger()
    ledger.record(tier="cheap", usage={"prompt_tokens": 10}, cache_hit=False, query_id="C01")
    assert "这个字段" not in ledger.to_jsonl()
    assert '"tier": "cheap"' in ledger.to_jsonl()


def test_weekly_report_lists_unpriced_items():
    report = cost.CostLedger().weekly_report()
    names = {x["item"] for x in report["unpriced_items"]}
    assert "向量库（pgvector）" in names and "网络出流量" in names
    assert all(x["status"] == "未计入" for x in report["unpriced_items"])


# ---- /chat 接线（默认关闭 → 命中路径）--------------------------------------
def test_chat_cache_miss_then_hit(monkeypatch):
    monkeypatch.setattr(main, "COST_CACHE_ENABLED", True)
    monkeypatch.setattr(main, "COST_LEDGER_ENABLED", True)
    cost.CACHE.clear()
    cost.LEDGER.entries.clear()
    client = TestClient(main.app)
    body = {"messages": _msg(), "model": "cheap"}
    r1 = client.post("/chat", json=body)
    r2 = client.post("/chat", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers.get("X-Cost-Cache") == "MISS"
    assert r2.headers.get("X-Cost-Cache") == "HIT"
    assert r2.json()["cost_cache"]["hit"] is True
    assert r1.json()["content"] == r2.json()["content"]
    # 命中这次没调模型：用量必须归零，且不能复用上一次的 request_id
    assert r2.json()["usage"]["total_tokens"] == 0
    assert r2.json()["usage"]["cached"] is True
    assert r2.json()["request_id"] != r1.json()["request_id"]
    assert cost.CACHE.stats.hits == 1
    hits = [e for e in cost.LEDGER.entries if e["cache_hit"]]
    assert len(hits) == 1 and hits[0]["total_tokens"] == 0 and hits[0]["cost_units"] == 0.0


def test_chat_cache_disabled_by_default(monkeypatch):
    monkeypatch.setattr(main, "COST_CACHE_ENABLED", False)
    monkeypatch.setattr(main, "COST_LEDGER_ENABLED", False)
    cost.CACHE.clear()
    client = TestClient(main.app)
    r = client.post("/chat", json={"messages": _msg(), "model": "cheap"})
    assert r.status_code == 200
    assert "X-Cost-Cache" not in r.headers
    assert "cost_cache" not in r.json()


def test_chat_multi_turn_is_not_cached(monkeypatch):
    monkeypatch.setattr(main, "COST_CACHE_ENABLED", True)
    monkeypatch.setattr(main, "COST_LEDGER_ENABLED", False)
    cost.CACHE.clear()
    client = TestClient(main.app)
    body = {
        "messages": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ],
        "model": "cheap",
    }
    client.post("/chat", json=body)
    client.post("/chat", json=body)
    assert cost.CACHE.stats.stores == 0
    assert cost.CACHE.stats.skipped.get("multi_turn") == 2
