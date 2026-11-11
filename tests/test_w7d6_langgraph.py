"""W7 D6 · 真 LangGraph Agent 单测：图结构、7 场景守卫、OTel span、checkpointer。"""

from __future__ import annotations

import json
import os

import agent_demo as legacy
import agent_graph as ag
import pytest
from langgraph.checkpoint.memory import InMemorySaver


def run(task: str, **kwargs):
    result, ctx = ag.run_agent(task, ctx=ag.local_tracing(), **kwargs)
    return result, ctx


def _loop(_state, _step):
    return ag.PlanStep("loop_tool", {"text": "再查一次"}, "模拟死循环：永远要求再查")


def _slow(_state, _step):
    return ag.PlanStep("slow_tool", {"text": "x"}, "慢工具", finish=True)


def _unknown(_state, _step):
    return ag.PlanStep("no_such_tool", {"text": "x"}, "故意点名不存在的工具", finish=True)


def _broken(_state, _step):
    return ag.PlanStep("broken_tool", {"text": "x"}, "会崩的工具", finish=True)


def _injection(_state, _step):
    return ag.PlanStep(
        "calculator", {"expr": "__import__('os').system('echo pwned')"}, "注入式表达式", finish=True
    )


def _span_rows(ctx):
    return list(ctx.spans())


# ---- 工具与规划 -------------------------------------------------------------
def test_tools_reuse_ast_whitelist():
    assert ag.calculator("128*4") == "CALC=512"
    assert ag.calculator("(3+5)/2") == "CALC=4.0"
    payload = "__import__('os').system('echo pwned')"
    assert ag.calculator(payload).startswith("CALC_ERROR")
    assert "pwned" not in ag.calculator(payload)


def test_default_plan_is_rule_based_and_ends_with_summarize():
    plan = ag.default_plan("算一下 128*4，并查报销截止日")
    assert [p.tool for p in plan] == ["calculator", "policy_lookup", "summarize"]
    assert plan[-1].finish is True
    assert plan[-1].reason == "事实收集完毕，拼成回答并结束"


# ---- 真的用了框架 -----------------------------------------------------------
def test_graph_uses_real_stategraph_nodes_and_checkpointer():
    saver = InMemorySaver()
    graph = ag.build_graph(ctx=ag.local_tracing(), checkpointer=saver)
    assert hasattr(graph, "get_graph"), "应是编译后的 StateGraph"
    assert {"plan", "tools", "observe"} <= set(graph.get_graph().nodes)
    assert getattr(graph, "checkpointer", None) is saver


def test_compiled_graph_has_toolnode():
    graph = ag.build_graph(ctx=ag.local_tracing())
    nodes = graph.get_graph().nodes
    assert "tools" in nodes
    assert getattr(nodes["tools"], "name", "") == "tools" or "ToolNode" in str(
        type(nodes["tools"].data)
    )


# ---- 正常多步 ---------------------------------------------------------------
def test_s1_three_steps_with_full_trace():
    r, _ctx = run("算一下 128*4，并查报销截止日")
    assert r.finish_reason == "completed"
    assert r.steps == 3
    assert [e.tool for e in r.trace] == ["calculator", "policy_lookup", "summarize"]
    assert "CALC=512" in r.answer
    for e in r.trace:
        assert e.tool and e.args and e.result and e.reason
        assert e.elapsed_ms >= 0
        assert e.ok


def test_s1_tool_spans_have_parent_and_full_attributes():
    _r, ctx = run("算一下 128*4，并查报销截止日")
    spans = _span_rows(ctx)
    root = next(s for s in spans if s.name == "agent.run")
    tools = [s for s in spans if s.name.startswith("agent.tool.")]
    assert len(spans) == 4
    assert len(tools) == 3
    assert [s.name for s in tools] == [
        "agent.tool.calculator",
        "agent.tool.policy_lookup",
        "agent.tool.summarize",
    ]
    for s in tools:
        assert s.parent is not None
        assert s.parent.span_id == root.context.span_id, "每个工具 span 必须挂在根 span 下"
        attrs = dict(s.attributes or {})
        assert {
            "agent.step", "agent.tool", "agent.args", "agent.reason",
            "agent.result", "agent.elapsed_ms", "agent.ok",
        } <= set(attrs)
        assert attrs["agent.elapsed_ms"] >= 0
        assert json.loads(attrs["agent.args"])


def test_unknown_tool_is_blocked_before_toolnode_so_no_tool_span():
    r, ctx = run("点名不存在的工具", planner=_unknown)
    assert r.finish_reason == "tool_error"
    assert r.trace[0].result == "UNKNOWN_TOOL"
    assert [s.name for s in _span_rows(ctx)] == ["agent.run"]


def test_trace_is_replayable_and_deterministic():
    a, _ = run("审批人是谁？")
    b, _ = run("审批人是谁？")

    def stable(r):
        return [
            {k: v for k, v in e.as_dict().items() if k != "elapsed_ms"}
            for e in r.trace
        ]

    assert stable(a) == stable(b)
    assert a.finish_reason == b.finish_reason == "completed"


def test_as_dict_contains_all_fields():
    r, _ = run("32/8 等于多少？")
    d = r.as_dict()
    assert {"task", "answer", "finish_reason", "steps", "elapsed_ms", "trace"} <= set(d)
    assert d["steps"] == 2


@pytest.mark.parametrize("task", ["审批人是谁？", "32/8 等于多少？"])
def test_no_trace_entry_missing_reason(task):
    r, _ = run(task)
    assert all(e.reason for e in r.trace)


# ---- 三条守卫 ---------------------------------------------------------------
def test_max_steps_stops_infinite_loop():
    r, ctx = run("一直查下去", tools=[ag.loop_tool], planner=_loop, max_steps=4)
    assert r.finish_reason == "max_steps"
    assert r.steps == 4
    assert len(_span_rows(ctx)) == 5, "1 个根 span + 4 个工具 span"


def test_timeout_stops_slow_tool():
    r, _ = run("慢工具", tools=[ag.slow_tool], planner=_slow, timeout_seconds=0.05)
    assert r.finish_reason == "timeout"
    assert r.steps >= 1


def test_unknown_tool_is_recorded_not_crashed():
    r, _ = run("x", planner=_unknown)
    assert r.finish_reason == "tool_error"
    assert r.trace[0].result == "UNKNOWN_TOOL"
    assert r.trace[0].ok is False


def test_failing_tool_is_recorded_not_crashed():
    r, _ = run("x", tools=[ag.broken_tool], planner=_broken)
    assert r.finish_reason == "tool_error"
    assert r.trace[0].result == "TOOL_ERROR=RuntimeError"


def test_injection_never_executes():
    r, _ = run("算一下这个", tools=[ag.calculator], planner=_injection)
    assert r.finish_reason == "completed"
    assert "CALC_ERROR" in r.trace[0].result
    assert not r.trace[0].result.startswith("pwned")


# ---- checkpointer 断点续跑 --------------------------------------------------
def test_resume_from_checkpoint_with_freshly_compiled_graphs():
    r, ctx, checkpoints = ag.run_with_resume("算一下 128*4，并查报销截止日")
    assert r.finish_reason == "completed"
    assert r.steps == 3
    assert checkpoints == ["tools", "tools", "tools"]
    assert len(_span_rows(ctx)) == 4


# ---- 与 11/09 零依赖实现逐项对账 -------------------------------------------
@pytest.mark.parametrize(
    "task",
    ["算一下 128*4，并查报销截止日", "审批人是谁？", "32/8 等于多少？"],
)
def test_same_finish_reason_and_tool_chain_as_legacy_agent(task):
    graph_result, _ = run(task)
    legacy_result = legacy.run_agent(task)
    assert graph_result.finish_reason == legacy_result.finish_reason
    assert [e.tool for e in graph_result.trace] == [e.tool for e in legacy_result.trace]
    assert [e.args for e in graph_result.trace] == [e.args for e in legacy_result.trace]


# ---- 本机接入 Langfuse 的两个坑（纯函数，不联网）--------------------------------
def test_normalize_langfuse_host_rewrites_localhost_to_ipv4():
    assert ag.normalize_langfuse_host("http://localhost:3000") == "http://127.0.0.1:3000"
    assert ag.normalize_langfuse_host("http://127.0.0.1:3000/") == "http://127.0.0.1:3000"
    assert ag.normalize_langfuse_host("http://langfuse.internal:3000") == (
        "http://langfuse.internal:3000"
    )


def test_bypass_local_proxy_merges_without_deleting(monkeypatch):
    monkeypatch.setenv("no_proxy", "example.com")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:6670")
    merged = ag.bypass_local_proxy()
    assert {"example.com", "127.0.0.1", "localhost"} <= set(merged.split(","))
    assert os.environ["NO_PROXY"] == merged
    assert os.environ["http_proxy"] == "http://127.0.0.1:6670", "不得删除既有代理变量"
