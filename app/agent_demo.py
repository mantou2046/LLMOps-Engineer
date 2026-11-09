"""W7 D6 · 最小可观测 Agent（纯标准库，零第三方依赖）。

⚠️ 边界：本机 2026-11-09 实测 `langgraph` / `langchain` / `langfuse` 全部未安装，
   所以这**不是 LangGraph 实现**，是「多步 Agent + 全链路 trace」概念的最小可跑演示：
   节点 / 状态 / 分支 / 工具循环都用普通函数表达，不装新包、不改服务、不接真实模型。
   真 LangGraph 的 StateGraph / ToolNode / checkpointer 需另装依赖后补。

设计纪律（沿用 W4 D6 分流器、W7 D5 决策器的同一条）：
  · **不用 LLM 判下一步**：规则判据微秒级、零成本、同输入永远同结论，可回放对账。
  · trace 记全：每步工具名、入参、结果、耗时、为什么选它、结束原因。
  · 三条守卫：最大步数、总超时、未知/异常工具。任务卡点名「agent 极易死循环」。
  · 工具不接触凭据，trace 不记请求正文之外的敏感值；calculator 用 AST 白名单求值，
    **不用 eval** —— 工具能执行任意代码是最经典的一类 Agent 安全事故。

流水线：plan（按任务定工具队列）→ act（逐步执行）→ observe（结果进状态）→
        finalize（summarize 收尾）→ trace 落盘。
"""

from __future__ import annotations

import ast
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

POLICY_BOOK: dict[str, str] = {
    "截止日": "字段=截止日；值=每月 25 日；依据=制度第 3 条。",
    "审批人": "字段=审批人；值=部门主管；依据=制度第 5 条。",
    "发票": "字段=发票；值=增值税专用；依据=制度第 7 条。",
    "超标": "字段=超标；值=需附说明；依据=制度第 9 条。",
}

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _safe_eval(node: ast.AST) -> float:
    """只允许数字与四则运算的 AST 节点；遇到别的直接拒绝。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _safe_eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    raise ValueError(f"不允许的表达式节点：{type(node).__name__}")


def calculator(expr: str) -> str:
    """工具 1：安全四则运算。用 AST 白名单，绝不 eval 用户文本。"""
    try:
        value = _safe_eval(ast.parse(expr, mode="eval"))
        return f"CALC={value}"
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
        return f"CALC_ERROR={type(exc).__name__}"


def policy_lookup(key: str) -> str:
    """工具 2：查一份固定制度小表（模拟 RAG 检索的返回）。"""
    for name, text in POLICY_BOOK.items():
        if name in key:
            return text
    return "NOT_FOUND"


def summarize(text: str) -> str:
    """工具 3：把已收集的事实拼成回答，不新增事实。"""
    facts = [x for x in text.split("|") if x]
    if not facts:
        return "资料不足，无法回答。"
    return "回答：" + "；".join(facts)


DEFAULT_TOOLS: dict[str, Callable[[str], str]] = {
    "calculator": calculator,
    "policy_lookup": policy_lookup,
    "summarize": summarize,
}


@dataclass(frozen=True)
class TraceEntry:
    step: int
    tool: str
    args: dict[str, Any]
    result: str
    elapsed_ms: float
    reason: str
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "reason": self.reason,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class Plan:
    tool: str
    args: dict[str, Any]
    reason: str
    finish: bool = False


@dataclass
class AgentState:
    task: str
    queue: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentResult:
    task: str
    answer: str
    finish_reason: str          # completed | max_steps | timeout | tool_error
    steps: int
    elapsed_ms: float
    trace: tuple[TraceEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "answer": self.answer,
            "finish_reason": self.finish_reason,
            "steps": self.steps,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "trace": [e.as_dict() for e in self.trace],
        }


def default_planner(state: AgentState, step: int) -> Plan:
    """规则规划：第一次按任务关键词排出工具队列，之后逐步取，取完用 summarize 收尾。

    有状态（队列写在 state 里），所以下一步取决于上一步执行结果后的剩余计划 ——
    这比「一次性 if-else 调三个函数」更像 Agent 的循环。
    """
    if step == 1:
        task = state.task
        if any(op in task for op in "+-*/×") and any(c.isdigit() for c in task):
            state.queue.append(("calculator", {"expr": _extract_expr(task)}, "任务含算式，先算数"))
        for key in POLICY_BOOK:
            if key in task:
                state.queue.append(
                    ("policy_lookup", {"key": key}, f"任务提到「{key}」，查制度原文")
                )
                break
    if state.queue:
        tool, args, reason = state.queue.pop(0)
        return Plan(tool=tool, args=args, reason=reason)
    return Plan(
        tool="summarize",
        args={"text": "|".join(state.facts)},
        reason="事实收集完毕，拼成回答并结束",
        finish=True,
    )


def _extract_expr(task: str) -> str:
    """从任务里抠出第一段像算式的子串，供 calculator 用。"""
    import re

    m = re.search(r"[\d\s\+\-\*/\(\)\.×÷]+", task)
    if not m:
        return "0"
    return m.group(0).strip().replace("×", "*").replace("÷", "/") or "0"


def run_agent(
    task: str,
    *,
    tools: dict[str, Callable[[str], str]] | None = None,
    planner: Callable[[AgentState, int], Plan] | None = None,
    max_steps: int = 6,
    timeout_seconds: float = 5.0,
) -> AgentResult:
    """跑一个多步 Agent，返回完整 trace。三条守卫保证不会死循环。"""
    toolset = tools if tools is not None else DEFAULT_TOOLS
    plan_fn = planner or default_planner
    state = AgentState(task=task)
    trace: list[TraceEntry] = []
    started = time.perf_counter()
    answer = ""
    finish_reason = "max_steps"

    for step in range(1, max_steps + 1):
        if time.perf_counter() - started > timeout_seconds:
            finish_reason = "timeout"
            break
        plan = plan_fn(state, step)
        fn = toolset.get(plan.tool)
        t0 = time.perf_counter()
        if fn is None:
            elapsed = (time.perf_counter() - t0) * 1000
            trace.append(
                TraceEntry(step, plan.tool, plan.args, "UNKNOWN_TOOL", elapsed, plan.reason, False)
            )
            finish_reason = "tool_error"
            break
        try:
            result = fn(**plan.args)
            ok = True
        except Exception as exc:  # 工具异常不能让 Agent 崩掉，记进 trace 再终止
            result = f"TOOL_ERROR={type(exc).__name__}"
            ok = False
        elapsed = (time.perf_counter() - t0) * 1000
        trace.append(TraceEntry(step, plan.tool, plan.args, result, elapsed, plan.reason, ok))
        state.facts.append(result)
        if not ok:
            finish_reason = "tool_error"
            break
        if time.perf_counter() - started > timeout_seconds:
            finish_reason = "timeout"
            break
        if plan.finish:
            answer = result
            finish_reason = "completed"
            break
    else:
        finish_reason = "max_steps"

    return AgentResult(
        task=task,
        answer=answer,
        finish_reason=finish_reason,
        steps=len(trace),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        trace=tuple(trace),
    )
