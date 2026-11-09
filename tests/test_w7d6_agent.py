"""W7 D6 · 最小可观测 Agent 单测：trace 完整性、三条守卫、工具安全。"""

from __future__ import annotations

import time

import agent_demo as ag
import pytest


# ---- 工具 ------------------------------------------------------------------
def test_calculator_basic_and_safe():
    assert ag.calculator("128*4") == "CALC=512"
    assert ag.calculator("(3+5)/2") == "CALC=4.0"
    assert ag.calculator("1/0").startswith("CALC_ERROR")


def test_calculator_rejects_code_injection():
    payload = "__import__('os').system('echo pwned')"
    result = ag.calculator(payload)
    assert result.startswith("CALC_ERROR")
    assert "pwned" not in result


def test_policy_lookup_hit_and_miss():
    assert "每月 25 日" in ag.policy_lookup("截止日")
    assert ag.policy_lookup("不存在的键") == "NOT_FOUND"


def test_summarize_without_facts_is_honest():
    assert ag.summarize("") == "资料不足，无法回答。"


# ---- 正常多步 --------------------------------------------------------------
def test_default_agent_three_steps_with_full_trace():
    r = ag.run_agent("算一下 128*4，并查报销截止日")
    assert r.finish_reason == "completed"
    assert r.steps == 3
    assert [e.tool for e in r.trace] == ["calculator", "policy_lookup", "summarize"]
    assert "CALC=512" in r.answer
    for e in r.trace:
        assert e.tool and e.args and e.result and e.reason
        assert e.elapsed_ms >= 0
        assert e.ok


def test_trace_is_replayable_and_deterministic():
    a = ag.run_agent("审批人是谁？")
    b = ag.run_agent("审批人是谁？")

    def stable(r):
        # elapsed_ms 是本机计时，每次必然不同；回放对账只看决策与结果
        return [
            {k: v for k, v in e.as_dict().items() if k != "elapsed_ms"}
            for e in r.trace
        ]

    assert stable(a) == stable(b)
    assert a.finish_reason == b.finish_reason == "completed"


def test_as_dict_contains_all_fields():
    d = ag.run_agent("32/8 等于多少？").as_dict()
    assert {"task", "answer", "finish_reason", "steps", "elapsed_ms", "trace"} <= set(d)
    assert d["steps"] == 2


# ---- 三条守卫 --------------------------------------------------------------
def test_max_steps_stops_infinite_loop():
    def loop(text: str) -> str:
        return f"NEED_MORE:{text}"

    def planner(_state, _step):
        return ag.Plan("loop", {"text": "x"}, "再查一次", finish=False)

    r = ag.run_agent("一直查", tools={"loop": loop}, planner=planner, max_steps=4)
    assert r.finish_reason == "max_steps"
    assert r.steps == 4


def test_timeout_stops_slow_tool():
    def slow(text: str) -> str:
        time.sleep(0.15)
        return f"done:{text}"

    def planner(_state, _step):
        return ag.Plan("slow", {"text": "x"}, "慢工具", finish=True)

    r = ag.run_agent(
        "慢", tools={"slow": slow}, planner=planner, timeout_seconds=0.05
    )
    assert r.finish_reason == "timeout"
    assert r.steps >= 1


def test_unknown_tool_is_recorded_not_crashed():
    def planner(_state, _step):
        return ag.Plan("ghost", {"text": "x"}, "点名不存在的工具", finish=True)

    r = ag.run_agent("x", planner=planner)
    assert r.finish_reason == "tool_error"
    assert r.trace[0].result == "UNKNOWN_TOOL"
    assert r.trace[0].ok is False


def test_failing_tool_is_recorded_not_crashed():
    def broken(text: str) -> str:
        raise RuntimeError(f"boom:{text}")

    def planner(_state, _step):
        return ag.Plan("broken", {"text": "x"}, "会崩的工具", finish=True)

    r = ag.run_agent("x", tools={"broken": broken}, planner=planner)
    assert r.finish_reason == "tool_error"
    assert r.trace[0].result == "TOOL_ERROR=RuntimeError"


@pytest.mark.parametrize("task", ["审批人是谁？", "32/8 等于多少？"])
def test_no_trace_entry_missing_reason(task):
    r = ag.run_agent(task)
    assert all(e.reason for e in r.trace)
