"""W7 D6 · 最小可观测 Agent 实跑：正常多步 + 三条守卫 + 工具安全。

⚠️ 纯标准库演示，不是 LangGraph（本机未装）。任务卡验收「trace 里能看到每一步的
   工具调用、入参与耗时」，本脚本逐条打印并落盘，可直接核对。

用法（仓库根目录）：
  D:\\python314\\python.exe scripts\\w7d6_agent_demo.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import agent_demo as ag  # noqa: E402

OUT = ROOT / "evals" / "out" / "w7d6" / "agent_demo.json"


def slow_tool(text: str) -> str:
    time.sleep(0.15)
    return f"SLOW_DONE:{text}"


def loop_tool(text: str) -> str:
    return f"NEED_MORE:{text}"


def always_loop(_state: ag.AgentState, _step: int) -> ag.Plan:
    return ag.Plan("loop_tool", {"text": "再查一次"}, "模拟死循环：永远要求再查", finish=False)


def unknown_tool(_state: ag.AgentState, _step: int) -> ag.Plan:
    return ag.Plan("no_such_tool", {"text": "x"}, "故意点名不存在的工具", finish=True)


def main() -> None:
    scenarios: list[dict] = []

    def add(sid: str, name: str, result: ag.AgentResult, expect: str) -> None:
        scenarios.append(
            {"id": sid, "name": name, "expected": expect, **result.as_dict()}
        )

    add(
        "S1", "算 128*4 并查报销截止日",
        ag.run_agent("算一下 128*4，并查报销截止日"),
        "completed",
    )
    add("S2", "只查审批人", ag.run_agent("审批人是谁？"), "completed")
    add("S3", "只算 32/8", ag.run_agent("32/8 等于多少？"), "completed")
    add(
        "S4", "死循环守卫（max_steps=4）",
        ag.run_agent(
            "一直查下去",
            tools={"loop_tool": loop_tool},
            planner=always_loop,
            max_steps=4,
        ),
        "max_steps",
    )
    add(
        "S5", "超时守卫（timeout=0.05s）",
        ag.run_agent(
            "慢工具",
            tools={"slow_tool": slow_tool},
            planner=lambda _s, _i: ag.Plan("slow_tool", {"text": "x"}, "慢工具", finish=True),
            timeout_seconds=0.05,
        ),
        "timeout",
    )
    add(
        "S6", "未知工具",
        ag.run_agent("点名不存在的工具", planner=unknown_tool),
        "tool_error",
    )
    add(
        "S7", "恶意表达式不被执行",
        ag.run_agent(
            "算一下这个",
            tools={"calculator": ag.calculator},
            planner=lambda _s, _i: ag.Plan(
                "calculator",
                {"expr": "__import__('os').system('echo pwned')"},
                "注入式表达式",
                finish=True,
            ),
        ),
        "completed",
    )

    payload = {
        "scenarios": scenarios,
        "guardrails": ["max_steps", "timeout_seconds", "unknown_or_failing_tool"],
        "tools": list(ag.DEFAULT_TOOLS),
        "caveats": [
            "纯标准库演示，不是 LangGraph；本机未装 langgraph / langchain / langfuse",
            "planner 是规则而非 LLM 判定，同输入永远同结论",
            "工具是本地固定表与四则运算，不接触真实模型、凭据或外部服务",
            "trace 只记入参与结果摘要，不记请求正文之外的敏感值",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ID   场景                        步数  结束原因     工具链")
    for s in scenarios:
        chain = " → ".join(e["tool"] for e in s["trace"]) or "（无）"
        print(f"{s['id']:<5} {s['name']:<26} {s['steps']:>3}  {s['finish_reason']:<12} {chain}")

    s1 = scenarios[0]
    print("\n=== S1 全链路 trace（任务卡验收点）===")
    for e in s1["trace"]:
        print(
            f"  step={e['step']} tool={e['tool']} args={e['args']} "
            f"result={e['result']} elapsed_ms={e['elapsed_ms']} reason={e['reason']}"
        )
    print(f"  answer={s1['answer']}")
    print(
        f"  finish_reason={s1['finish_reason']} steps={s1['steps']} "
        f"elapsed_ms={s1['elapsed_ms']}"
    )

    # ---- 断言 --------------------------------------------------------------
    by_id = {s["id"]: s for s in scenarios}
    assert all(s["finish_reason"] == s["expected"] for s in scenarios), "结束原因必须与预期一致"
    assert s1["steps"] == 3, "S1 应走 calculator → policy_lookup → summarize 三步"
    assert [e["tool"] for e in s1["trace"]] == ["calculator", "policy_lookup", "summarize"]
    assert all(
        {"step", "tool", "args", "result", "elapsed_ms", "reason", "ok"} <= set(e)
        for s in scenarios for e in s["trace"]
    ), "每条 trace 必须字段齐全"
    assert "CALC=512" in s1["trace"][0]["result"], "128*4 应算出 512"
    assert by_id["S4"]["steps"] == 4, "死循环必须在 max_steps 处停下"
    assert by_id["S5"]["steps"] >= 1, "超时场景至少记录到慢工具那一步"
    assert "CALC_ERROR" in by_id["S7"]["trace"][0]["result"], "注入表达式必须被 AST 白名单拒绝"
    assert not by_id["S7"]["trace"][0]["result"].startswith("pwned")
    print(f"\n断言 9/9 通过。明细：{OUT.relative_to(ROOT)}")
    print("⚠️ 这不是 LangGraph 实现；真 StateGraph / ToolNode / checkpointer 需装依赖后补。")


if __name__ == "__main__":
    main()
