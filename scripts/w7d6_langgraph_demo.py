"""W7 D6 · 真 LangGraph Agent 实跑（2026-11-11 补）：7 场景 + 真 span + 断点续跑。

和 11/09 的 `scripts/w7d6_agent_demo.py` 是同一组场景，结果逐项对账；
这份额外验证 OTel span 的父子层级与属性，以及 checkpointer 的断点续跑。

用法（仓库根目录）：
  D:\\python314\\python.exe scripts\\w7d6_langgraph_demo.py
  D:\\python314\\python.exe scripts\\w7d6_langgraph_demo.py --langfuse
     额外把 S1 的 span 真上报到自托管 Langfuse（需 health 200 且凭据有效）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import agent_demo as legacy  # noqa: E402
import agent_graph as ag  # noqa: E402

OUT_DIR = ROOT / "evals" / "out" / "w7d6"
OUT = OUT_DIR / "langgraph_demo.json"
SPAN_OUT = OUT_DIR / "langgraph_spans.json"


def _loop(_state, _step):
    return ag.PlanStep("loop_tool", {"text": "再查一次"}, "模拟死循环：永远要求再查")


def _slow(_state, _step):
    return ag.PlanStep("slow_tool", {"text": "x"}, "慢工具", finish=True)


def _unknown(_state, _step):
    return ag.PlanStep("no_such_tool", {"text": "x"}, "故意点名不存在的工具", finish=True)


def _injection(_state, _step):
    return ag.PlanStep(
        "calculator",
        {"expr": "__import__('os').system('echo pwned')"},
        "注入式表达式",
        finish=True,
    )


def _broken(_state, _step):
    return ag.PlanStep("broken_tool", {"text": "x"}, "会崩的工具", finish=True)


def span_rows(spans) -> list[dict]:
    rows = []
    for s in spans:
        attrs = dict(s.attributes or {})
        rows.append(
            {
                "name": s.name,
                "span_id": format(s.context.span_id, "016x"),
                "parent_span_id": (
                    format(s.parent.span_id, "016x") if s.parent is not None else None
                ),
                "duration_ms": round((s.end_time - s.start_time) / 1e6, 3),
                "attributes": attrs,
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="W7 D6 真 LangGraph Agent 实跑")
    ap.add_argument(
        "--langfuse",
        action="store_true",
        help="把 S1 的 span 真上报到自托管 Langfuse（需 health 200 且凭据有效）",
    )
    args = ap.parse_args()
    scenarios: list[dict] = []
    all_spans: list[dict] = []

    def add(
        sid: str,
        name: str,
        expect: str,
        *,
        tools=None,
        planner=None,
        max_steps: int = 6,
        timeout_seconds: float = 5.0,
    ) -> None:
        ctx = ag.local_tracing()
        result, ctx = ag.run_agent(
            name,
            tools=tools,
            planner=planner,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            ctx=ctx,
        )
        rows = span_rows(ctx.spans())
        all_spans.extend({"scenario": sid, **r} for r in rows)
        scenarios.append(
            {
                "id": sid,
                "name": name,
                "expected": expect,
                **result.as_dict(),
                "span_count": len(rows),
                "tool_spans": [r["name"] for r in rows if r["name"].startswith("agent.tool.")],
            }
        )

    add("S1", "算一下 128*4，并查报销截止日", "completed")
    add("S2", "审批人是谁？", "completed")
    add("S3", "32/8 等于多少？", "completed")
    add(
        "S4", "一直查下去", "max_steps",
        tools=[ag.loop_tool], planner=_loop, max_steps=4,
    )
    add(
        "S5", "慢工具", "timeout",
        tools=[ag.slow_tool], planner=_slow, timeout_seconds=0.05,
    )
    add("S6", "点名不存在的工具", "tool_error", planner=_unknown)
    add("S7", "算一下这个", "completed", tools=[ag.calculator], planner=_injection)
    add(
        "S8", "会崩的工具", "tool_error", tools=[ag.broken_tool], planner=_broken,
    )

    resume_result, resume_ctx, checkpoints = ag.run_with_resume("算一下 128*4，并查报销截止日")

    # ---- 新旧实现对账 ------------------------------------------------------
    legacy_rows = []
    legacy_rows.append(legacy.run_agent("算一下 128*4，并查报销截止日"))
    legacy_rows.append(legacy.run_agent("审批人是谁？"))
    legacy_rows.append(legacy.run_agent("32/8 等于多少？"))

    payload = {
        "implementation": "langgraph-1.2.20 + langfuse-3.15.0 (OTel span)",
        "date": "2026-11-11",
        "scenarios": scenarios,
        "resume": {
            "finish_reason": resume_result.finish_reason,
            "steps": resume_result.steps,
            "checkpoints": checkpoints,
            "span_count": len(resume_ctx.spans()),
        },
        "legacy_crosscheck": [
            {
                "id": s["id"],
                "graph": s["finish_reason"],
                "legacy": r.finish_reason,
                "same_tools": [e["tool"] for e in s["trace"]] == [e.tool for e in r.trace],
            }
            for s, r in zip(scenarios[:3], legacy_rows, strict=True)
        ],
        "caveats": [
            (
                "默认用独立本地 provider 验 span 结构，保证零网络可复现；"
                "加 --langfuse 才真上报到自托管 Langfuse"
            ),
            "规划器是规则而非 LLM 判定，同输入永远同结论",
            "工具是本地固定表与四则运算，不接触真实模型、凭据或外部服务",
            "elapsed_ms 是本机 CPU 时间，回放对账不比较耗时",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    SPAN_OUT.write_text(json.dumps(all_spans, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ID   场景                        步数  结束原因     span数  工具链")
    for s in scenarios:
        chain = " → ".join(e["tool"] for e in s["trace"]) or "（无）"
        print(
            f"{s['id']:<5} {s['name']:<26} {s['steps']:>3}  "
            f"{s['finish_reason']:<12} {s['span_count']:>3}    {chain}"
        )

    s1 = scenarios[0]
    print("\n=== S1 全链路 trace（任务卡验收点）===")
    for e in s1["trace"]:
        print(
            f"  step={e['step']} tool={e['tool']} args={e['args']} "
            f"result={e['result']} elapsed_ms={e['elapsed_ms']} reason={e['reason']}"
        )
    print(f"  answer={s1['answer']}")
    print(f"  finish_reason={s1['finish_reason']} steps={s1['steps']}")

    s1_spans = [r for r in all_spans if r["scenario"] == "S1"]
    root = next(r for r in s1_spans if r["name"] == "agent.run")
    print("\n=== S1 真 OTel span（父子层级）===")
    for r in s1_spans:
        a = r["attributes"]
        print(
            f"  {r['name']:<28} parent={r['parent_span_id']} "
            f"duration_ms={r['duration_ms']} tool={a.get('agent.tool', '-')} "
            f"step={a.get('agent.step', '-')} elapsed_ms={a.get('agent.elapsed_ms', '-')}"
        )

    print("\n=== checkpointer 断点续跑 ===")
    print(
        f"  checkpoints={checkpoints} finish_reason={resume_result.finish_reason} "
        f"steps={resume_result.steps} spans={len(resume_ctx.spans())}"
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
    assert (
        by_id["S8"]["trace"][0]["result"] == "TOOL_ERROR=RuntimeError"
    ), "异常工具应被 ToolNode 兜住"

    # ---- 新增：真 span 断言 -------------------------------------------------
    tool_spans = [r for r in s1_spans if r["name"].startswith("agent.tool.")]
    assert len(s1_spans) == 4, "S1 应有 1 个根 span + 3 个工具 span"
    assert len(tool_spans) == 3
    assert all(
        r["parent_span_id"] == root["span_id"] for r in tool_spans
    ), "工具 span 必须挂在根 span 下"
    assert [r["name"] for r in tool_spans] == [
        "agent.tool.calculator", "agent.tool.policy_lookup", "agent.tool.summarize"
    ]
    for r in tool_spans:
        a = r["attributes"]
        assert {"agent.step", "agent.tool", "agent.args", "agent.reason", "agent.result",
                "agent.elapsed_ms", "agent.ok"} <= set(a), "每个工具 span 属性必须齐全"
        assert a["agent.elapsed_ms"] >= 0
    crosscheck = payload["legacy_crosscheck"]
    assert all(
        x["graph"] == x["legacy"] and x["same_tools"] for x in crosscheck
    ), "新旧实现前三个场景的结束原因与工具链必须一致"

    # ---- 新增：checkpointer 断言 -------------------------------------------
    assert resume_result.finish_reason == "completed", "断点续跑后应正常完成"
    assert resume_result.steps == 3, "续跑不得重跑已完成的步骤"
    assert checkpoints == ["tools", "tools", "tools"], "三次工具调用前各停一次"
    assert len(resume_ctx.spans()) == 4, "续跑链路也应有 1 根 + 3 工具 span"

    print(
        f"\n断言 24/24 通过。明细：{OUT.relative_to(ROOT)}、{SPAN_OUT.relative_to(ROOT)}"
    )
    if args.langfuse:
        print("8 场景仍由本地 InMemorySpanExporter 校验；下面额外用全局 tracer 上报 S1。")
    else:
        print("默认路径：span 由本地 InMemorySpanExporter 验证，不联网。")

    if args.langfuse:
        tracer = ag.enable_langfuse_tracing()
        if tracer is None:
            print("⚠️ Langfuse 不可用（凭据缺失 / health 不通），本次未上报。")
            return
        cloud, _ = ag.run_agent("算一下 128*4，并查报销截止日")
        flushed = ag.flush_langfuse()
        print(
            f"✅ 已用全局 tracer 上报 S1：finish_reason={cloud.finish_reason} "
            f"steps={cloud.steps} flush={flushed}"
        )
        print("   面板查法：UI Traces 里找 agent.run，或 GET /api/public/v2/observations")


if __name__ == "__main__":
    main()
