"""app/routing.py 的判档单测。

覆盖：显式指定 / 长度加分 / 多轮 / 强词 / 弱词封顶 / 简单词负分 / 引用材料 /
阈值边界。全部用「已知输入 → 期望结论」断言，不测实现细节。
"""

from __future__ import annotations

import routing


def _msgs(*contents: str) -> list[dict]:
    return [{"role": "user", "content": c} for c in contents]


def test_explicit_primary_bypasses_rules():
    """显式指定 primary 时，哪怕是超短问题也不判，直接照办。"""
    d = routing.decide(_msgs("在吗"), explicit="primary")
    assert d.tier == "primary"
    assert d.explicit is True
    assert d.signals == ("explicit",)


def test_explicit_cheap_bypasses_rules():
    """显式指定 cheap 时，哪怕是超长难问也不判。"""
    long_q = "请对比 A 和 B 两个方案的合规风险，并分析取舍，给出优化建议 " * 10
    d = routing.decide(_msgs(long_q), explicit="cheap")
    assert d.tier == "cheap"
    assert d.explicit is True


def test_short_plain_question_defaults_cheap():
    """超短、无信号词的普通问题 → cheap。"""
    d = routing.decide(_msgs("这个字段怎么填"))
    assert d.tier == "cheap"
    assert "simple" in d.signals  # 「怎么填」命中简单词，减 1 分


def test_strong_keyword_forces_primary():
    """「对比」这类强信号词出现 → 直接 primary。"""
    d = routing.decide(_msgs("请对比两个方案的差异"))
    assert d.tier == "primary"
    assert any(s.startswith("strong") for s in d.signals)


def test_long_question_gets_length_bonus():
    """超长题面（≥120 字）→ 加 2 分，即便没关键词也可能够到 primary。"""
    long_q = "请一步一步说明这笔付款的完整审批流程，包括各级审批人的权限边界、超时退回机制、以及异常时的升级路径。 " * 4
    assert len(long_q) >= 120
    d = routing.decide(_msgs(long_q))
    assert any(s.startswith("len>=") for s in d.signals)


def test_multiturn_adds_one():
    """多轮（≥3 条消息）→ 加 1 分。"""
    d = routing.decide(
        [{"role": "user", "content": x} for x in ["第一问", "第二问", "第三问，上下文很长了"]]
    )
    assert any(s.startswith("turns=") for s in d.signals)


def test_simple_word_does_not_cancel_strong():
    """「是什么」和「对比」同时出现时，简单词负分不得抵消强词。"""
    d = routing.decide(_msgs("A 和 B 是什么关系？请对比两者的记账时点"))
    assert d.tier == "primary"
    # 绝不能因为「是什么」-1 就把强词压掉


def test_weak_keywords_cap_at_two():
    """弱词每个 +1 但封顶 +2，多个弱词不会无限加。"""
    q = "请说明这个条款的影响、原因、依据和关系"
    d = routing.decide(_msgs(q))
    weak = [s for s in d.signals if s.startswith("weak")]
    assert weak, "应命中弱信号词"
    # 4 个弱词只算 2 分（weak=2）


def test_reference_keyword_boosts():
    """「根据以下材料」→ 引用材料 +2。"""
    d = routing.decide(_msgs("根据以下材料回答问题：" + "。" * 10))
    assert "ref" in d.signals


def test_structured_alone_can_tip_to_primary():
    """结构化 +3 单独就够到阈值（3≥3）→ primary。"""
    d = routing.decide(_msgs("返回一个 JSON"), structured=True)
    assert d.tier == "primary"
    assert "structured" in d.signals


def test_score_is_reproducible():
    """同一输入两次判档，结论和 score 完全一致（可回放）。"""
    q = _msgs("请对比 A 和 B 方案的差异")
    a, b = routing.decide(q), routing.decide(q)
    assert (a.tier, a.score, a.signals) == (b.tier, b.score, b.signals)


def test_decision_carries_detail():
    """RouteDecision 一定带 detail，出问题时能说明「凭什么判的」。"""
    d = routing.decide(_msgs("在吗"))
    assert d.detail  # 非空字符串
