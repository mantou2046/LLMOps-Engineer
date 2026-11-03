"""app/llm.py 在 mock 模式下的策略单测。

mock 上游不发真实请求，但重试 / 结构化 / 降级都是真代码路径 —— 这正是不联网
也能把「最贵的逻辑」锁死的方式（W2 D8 就定下的可验收思路）。
"""

from __future__ import annotations

import llm


def test_mock_chat_returns_text():
    """默认 mock → 返回一段文本，mode 是 mock，attempts=1。"""
    out = llm.chat(messages=[{"role": "user", "content": "你好"}], tier_name="primary")
    assert out.mode == "mock"
    assert isinstance(out.content, str) and len(out.content) > 0
    assert out.attempts == 1 and out.retries == 0


def test_mock_chat_structured_returns_dict():
    """structured=True → content 是 dict（mock 会产合法 JSON）。"""
    out = llm.chat(
        messages=[{"role": "user", "content": "给一个 JSON"}],
        tier_name="primary",
        structured=True,
    )
    assert isinstance(out.content, dict)
    assert "answer" in out.content
    assert out.parse_failed is False


def test_mock_retry_on_injected_500():
    """MOCK_FAIL_N_TIMES=2 → 前两次 500，第 3 次成功，attempts=3 retries=2。

    ⚠️ mock 的故障注入用模块级全局计数器 `_mock_calls`（见 app/llm.py），
    会跨测试累加 —— 必须先在测试里清零，否则 MOCK_FAIL_N_TIMES 失效。
    """
    llm._mock_calls = 0  # 重置全局计数器（真实踩坑：不重置会因前面测试累加而失效）
    llm.MOCK_FAIL_N_TIMES = 2
    try:
        out = llm.chat(messages=[{"role": "user", "content": "重试测试"}], tier_name="primary")
        assert out.attempts == 3
        assert out.retries == 2
    finally:
        llm.MOCK_FAIL_N_TIMES = 0  # 还原，别污染其他测试


def test_mock_fallback_degrade():
    """primary 档注入 500 + fallback 就绪 → 降级成功，degraded=True。"""
    llm._mock_calls = 0  # 重置全局计数器（同上）
    llm.MOCK_FAIL_N_TIMES = 3  # primary 全部重试耗尽
    try:
        out = llm.chat(messages=[{"role": "user", "content": "降级测试"}], tier_name="primary")
        assert out.degraded is True
        assert out.degraded_from == "primary"
    finally:
        llm.MOCK_FAIL_N_TIMES = 0
