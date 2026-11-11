"""W7 D6 · 真 LangGraph Agent（2026-11-11 补）：StateGraph + ToolNode + checkpointer + OTel span。

和 `app/agent_demo.py` 的关系：那份是 11/09 的零依赖最小演示，**保留不动**做历史快照；
本文件是装了 `langgraph` / `langfuse` 之后重写的正式实现，两者返回同一种 `AgentResult`，
7 个场景可以逐项对账。

设计纪律（沿用 W4 D6 分流器、W7 D5 决策器、11/09 最小 Agent 的同一条）：
  · 不用 LLM 判下一步：规则规划器排工具队列，微秒级、零成本、同输入永远同结论。
  · 状态驱动：队列写进 StateGraph 的 state，下一步取决于上一步工具结果。
  · 三条守卫：最大步数、总超时、未知/异常工具。任务卡点名「agent 极易死循环」。
  · 工具不接触凭据；calculator 复用 AST 白名单求值，不用 eval。

观测口径（重要，别读成「已上报 Langfuse」）：
  · 每一步工具调用是**真正的 OpenTelemetry span**，由 ToolNode 的 wrap_tool_call 包住
    实际执行区间，属性带 step / tool / args / result / elapsed_ms / reason / ok。
  · 根 span 是 `agent.run`，子 span 是 `agent.tool.<name>`，父子关系可在导出的 span 里核。
  · Langfuse SDK 3.x 注册的是全局 TracerProvider；默认 `trace.get_tracer()` 拿到的 tracer
    在服务可达、凭据有效时会被自动采集上报。W2 的 `app/observability.py` 已用同一套 v3 API。
  · 本机 2026-11-11 自托管 Langfuse 服务未启动（health 连接被重置），所以实跑注入的是
    **独立本地 TracerProvider + InMemorySpanExporter**，只验 span 结构与层级，不冒充云端上报。
  · 字典 trace 是 span 的本地投影，供回放对账与旧断言使用；观测原语是 span，不是字典。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from operator import add
from typing import Annotated, Any, TypedDict

import agent_demo as legacy
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

__all__ = [
    "DEFAULT_TOOLS",
    "PlanStep",
    "TracingContext",
    "build_graph",
    "calculator",
    "default_plan",
    "enable_langfuse_tracing",
    "local_tracing",
    "loop_tool",
    "run_agent",
    "run_with_resume",
    "slow_tool",
]


# ---------------------------------------------------------------------------
# 工具：普通函数带类型注解，ToolNode 会自动 coerce 成 tool
# ---------------------------------------------------------------------------
def calculator(expr: str) -> str:
    """安全四则运算（AST 白名单，拒绝任意代码执行）。"""
    return legacy.calculator(expr)


def policy_lookup(key: str) -> str:
    """查一张固定制度小表，模拟 RAG 检索的返回。"""
    return legacy.policy_lookup(key)


def summarize(text: str) -> str:
    """把已收集的事实拼成回答，不新增事实。"""
    return legacy.summarize(text)


def loop_tool(text: str) -> str:
    """守卫演示用：永远要求再查一次。"""
    return f"NEED_MORE:{text}"


def slow_tool(text: str) -> str:
    """守卫演示用：故意睡 0.15 s。"""
    time.sleep(0.15)
    return f"SLOW_DONE:{text}"


def broken_tool(text: str) -> str:
    """守卫演示用：故意抛异常。"""
    raise RuntimeError(f"boom:{text}")


DEFAULT_TOOLS: list[Callable[..., str]] = [calculator, policy_lookup, summarize]


# ---------------------------------------------------------------------------
# 规划与状态
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanStep:
    tool: str
    args: dict[str, Any]
    reason: str
    finish: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "reason": self.reason, "finish": self.finish}


Planner = Callable[[dict[str, Any], int], PlanStep | None]


def default_plan(task: str) -> list[PlanStep]:
    """规则规划器：按任务关键词排工具队列，最后一步 summarize 收尾。"""
    steps: list[PlanStep] = []
    if any(op in task for op in "+-*/×÷") and any(c.isdigit() for c in task):
        steps.append(
            PlanStep("calculator", {"expr": legacy._extract_expr(task)}, "任务含算式，先算数")
        )
    for key in legacy.POLICY_BOOK:
        if key in task:
            steps.append(
                PlanStep("policy_lookup", {"key": key}, f"任务提到「{key}」，查制度原文")
            )
            break
    steps.append(PlanStep("summarize", {}, "事实收集完毕，拼成回答并结束", finish=True))
    return steps


class GraphState(TypedDict, total=False):
    task: str
    messages: Annotated[list[AnyMessage], add]
    queue: list[dict[str, Any]]
    pending: dict[str, Any]
    facts: list[str]
    trace: list[dict[str, Any]]
    steps: int
    answer: str
    finish_reason: str
    max_steps: int
    timeout_seconds: float
    started: float
    t0: float
    tool_names: list[str]


@dataclass
class TracingContext:
    """一次实跑的 tracer 与本地导出器，不设全局 provider，避免污染别的测试。"""

    provider: Any  # 本地是 TracerProvider；全局路径可能是 ProxyTracerProvider
    exporter: InMemorySpanExporter
    tracer: Tracer
    step_ms: dict[str, float] = field(default_factory=dict)

    def spans(self) -> list[Any]:
        return list(self.exporter.get_finished_spans())


def local_tracing() -> TracingContext:
    """独立本地 provider：真 OTel span，导出到内存供断言，不联网。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return TracingContext(provider, exporter, provider.get_tracer("llmops.w7d6.agent"))


def enable_langfuse_tracing() -> Tracer | None:
    """可选：初始化 Langfuse SDK v3 全局 TracerProvider，返回该全局 tracer。

    之后 `run_agent()` 不传 ctx 就会用全局 tracer，span 由 Langfuse 的 OTel exporter 采集。
    ⚠️ 需要 .env 里 LANGFUSE_* 三件套且自托管服务可达；不可用时返回 None，
       调用方应回落到 local_tracing()，不要谎称已上报。初始化不打印任何凭据值。
    """
    import os

    from dotenv import load_dotenv
    from langfuse import Langfuse

    load_dotenv()
    keys = (
        os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        os.getenv("LANGFUSE_SECRET_KEY", ""),
        os.getenv("LANGFUSE_HOST", ""),
    )
    if not all(keys):
        return None
    try:
        Langfuse(
            public_key=keys[0],
            secret_key=keys[1],
            host=keys[2],
            blocked_instrumentation_scopes=["fastapi"],
        )
    except Exception:  # noqa: BLE001 - 观测不可用时业务照跑
        return None
    return otel_trace.get_tracer("llmops.w7d6.agent")


def _tool_error(exc: Exception) -> str:
    """工具异常转成字符串进 ToolMessage，不让图崩掉。"""
    return f"TOOL_ERROR={type(exc).__name__}"


def _tool_names(tools: list[Any]) -> list[str]:
    return [getattr(t, "name", None) or getattr(t, "__name__", str(t)) for t in tools]


def _new_state(
    task: str,
    *,
    tools: list[Any],
    max_steps: int,
    timeout_seconds: float,
    queue: list[PlanStep] | None,
) -> GraphState:
    return GraphState(
        task=task,
        messages=[],
        queue=[s.as_dict() for s in (queue if queue is not None else default_plan(task))],
        pending={},
        facts=[],
        trace=[],
        steps=0,
        answer="",
        finish_reason="",
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        started=time.perf_counter(),
        t0=time.perf_counter(),
        tool_names=_tool_names(tools),
    )


# ---------------------------------------------------------------------------
# 图：START → plan → (tools → observe) × N → END
# ---------------------------------------------------------------------------
def build_graph(
    *,
    ctx: TracingContext,
    tools: list[Any] | None = None,
    planner: Planner | None = None,
    checkpointer: InMemorySaver | None = None,
    interrupt_before: list[str] | None = None,
):
    """编译一张带 checkpointer 的 StateGraph。planner 返回 PlanStep 可覆盖队列。"""
    toolset = tools if tools is not None else DEFAULT_TOOLS
    names = _tool_names(toolset)

    def span_wrap(request: ToolCallRequest, call_next):
        call = request.tool_call
        raw_state = request.state
        pending = raw_state.get("pending", {}) if isinstance(raw_state, dict) else {}
        args = call.get("args", {}) or {}
        with ctx.tracer.start_as_current_span(
            f"agent.tool.{call['name']}",
            attributes={
                "agent.step": int(pending.get("step", 0)),
                "agent.tool": call["name"],
                "agent.args": json.dumps(args, ensure_ascii=False),
                "agent.reason": str(pending.get("reason", "")),
            },
        ) as span:
            t0 = time.perf_counter()
            message = call_next(request)
            elapsed = (time.perf_counter() - t0) * 1000
            result = str(getattr(message, "content", ""))
            span.set_attribute("agent.elapsed_ms", round(elapsed, 3))
            span.set_attribute("agent.result", result[:512])
            span.set_attribute("agent.ok", not result.startswith(("TOOL_ERROR", "UNKNOWN_TOOL")))
            ctx.step_ms[str(call.get("id", ""))] = elapsed
            return message

    tool_node = ToolNode(
        toolset,
        handle_tool_errors=_tool_error,
        wrap_tool_call=span_wrap,
    )

    def plan(state: GraphState) -> dict[str, Any]:
        if state.get("finish_reason"):
            return {}
        step = state["steps"] + 1
        if time.perf_counter() - state["started"] > state["timeout_seconds"]:
            return {"finish_reason": "timeout"}
        if step > state["max_steps"]:
            return {"finish_reason": "max_steps"}

        custom = planner(state, step) if planner is not None else None
        if custom is not None:
            item = custom.as_dict()
            queue = list(state.get("queue", []))
        else:
            queue = list(state.get("queue", []))
            if not queue:
                return {"finish_reason": "completed"}
            item = queue.pop(0)

        if item["tool"] == "summarize" and not item["args"]:
            item = {**item, "args": {"text": "|".join(state.get("facts", []))}}

        if item["tool"] not in names:
            # 规划层就拦下未知工具，不进 ToolNode，因此这一步没有 tool span
            entry = legacy.TraceEntry(
                step=step,
                tool=item["tool"],
                args=item["args"],
                result="UNKNOWN_TOOL",
                elapsed_ms=0.0,
                reason=item["reason"],
                ok=False,
            )
            return {
                "queue": queue,
                "trace": [*state.get("trace", []), entry.as_dict()],
                "steps": step,
                "finish_reason": "tool_error",
            }

        call_id = f"call-{step}-{uuid.uuid4().hex[:8]}"
        tool_call = {
            "name": item["tool"],
            "args": item["args"],
            "id": call_id,
            "type": "tool_call",
        }
        ai = AIMessage(content="", tool_calls=[tool_call])
        return {
            "messages": [ai],
            "queue": queue,
            "pending": {**item, "step": step, "call_id": call_id},
            "t0": time.perf_counter(),
        }

    def observe(state: GraphState) -> dict[str, Any]:
        pending = state.get("pending", {})
        last = state["messages"][-1]
        result = str(getattr(last, "content", ""))
        elapsed = ctx.step_ms.get(pending.get("call_id", ""), 0.0)
        ok = not result.startswith(("TOOL_ERROR", "UNKNOWN_TOOL"))
        entry = legacy.TraceEntry(
            step=int(pending.get("step", state["steps"] + 1)),
            tool=str(pending.get("tool", "")),
            args=dict(pending.get("args", {})),
            result=result,
            elapsed_ms=elapsed,
            reason=str(pending.get("reason", "")),
            ok=ok,
        )
        facts = [*state.get("facts", []), result]
        trace = [*state.get("trace", []), entry.as_dict()]
        update: dict[str, Any] = {
            "facts": facts,
            "trace": trace,
            "steps": int(pending.get("step", state["steps"] + 1)),
            "pending": {},
        }
        if not ok:
            update["finish_reason"] = "tool_error"
        elif time.perf_counter() - state["started"] > state["timeout_seconds"]:
            update["finish_reason"] = "timeout"
        elif pending.get("finish"):
            update["finish_reason"] = "completed"
            update["answer"] = result
        return update

    def route_after_plan(state: GraphState) -> str:
        return "end" if state.get("finish_reason") else "tools"

    def route_after_observe(state: GraphState) -> str:
        return "end" if state.get("finish_reason") else "plan"

    builder = StateGraph(GraphState)
    builder.add_node("plan", plan)
    builder.add_node("tools", tool_node)
    builder.add_node("observe", observe)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", route_after_plan, {"tools": "tools", "end": END})
    builder.add_edge("tools", "observe")
    builder.add_conditional_edges("observe", route_after_observe, {"plan": "plan", "end": END})
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def _to_result(state: GraphState) -> legacy.AgentResult:
    entries = tuple(
        legacy.TraceEntry(
            step=e["step"],
            tool=e["tool"],
            args=e["args"],
            result=e["result"],
            elapsed_ms=e["elapsed_ms"],
            reason=e["reason"],
            ok=e["ok"],
        )
        for e in state.get("trace", [])
    )
    return legacy.AgentResult(
        task=state["task"],
        answer=state.get("answer", ""),
        finish_reason=state.get("finish_reason") or "max_steps",
        steps=state["steps"],
        elapsed_ms=(time.perf_counter() - state["started"]) * 1000,
        trace=entries,
    )


def run_agent(
    task: str,
    *,
    tools: list[Any] | None = None,
    planner: Planner | None = None,
    queue: list[PlanStep] | None = None,
    max_steps: int = 6,
    timeout_seconds: float = 5.0,
    ctx: TracingContext | None = None,
    checkpointer: InMemorySaver | None = None,
) -> tuple[legacy.AgentResult, TracingContext]:
    """跑一次真 LangGraph Agent，返回 (结果, tracing 上下文)。

    ctx 传 None 时用全局 tracer（Langfuse 初始化后会自动采集）；
    实跑与单测传 local_tracing()，可导出真 span 逐条核验。
    """
    tracing = ctx if ctx is not None else TracingContext(
        provider=otel_trace.get_tracer_provider(),  # type: ignore[arg-type]
        exporter=InMemorySpanExporter(),
        tracer=otel_trace.get_tracer("llmops.w7d6.agent"),
    )
    toolset = tools if tools is not None else DEFAULT_TOOLS
    initial = _new_state(
        task,
        tools=toolset,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
        queue=queue,
    )
    graph = build_graph(ctx=tracing, tools=toolset, planner=planner, checkpointer=checkpointer)
    config: dict[str, Any] = {
        "configurable": {"thread_id": uuid.uuid4().hex},
        "recursion_limit": max_steps * 4 + 4,
    }
    with tracing.tracer.start_as_current_span(
        "agent.run",
        attributes={
            "agent.task": task,
            "agent.max_steps": max_steps,
            "agent.timeout_seconds": timeout_seconds,
        },
    ) as root:
        final = graph.invoke(initial, config=config)
        root.set_attribute("agent.finish_reason", final.get("finish_reason") or "max_steps")
        root.set_attribute("agent.steps", int(final["steps"]))
    return _to_result(final), tracing


def run_with_resume(
    task: str,
    *,
    max_resumes: int = 8,
) -> tuple[legacy.AgentResult, TracingContext, list[str]]:
    """checkpointer 断点续跑演示：每次进 tools 前停，且用**新编译的图**读同一份状态。

    返回的第三个值是每次断点看到的 next 节点名列表，用来证明状态确实来自 checkpointer，
    而不是图对象自己的局部变量。
    """
    tracing = local_tracing()
    saver = InMemorySaver()
    toolset = DEFAULT_TOOLS
    initial = _new_state(task, tools=toolset, max_steps=6, timeout_seconds=5.0, queue=None)
    config: dict[str, Any] = {
        "configurable": {"thread_id": "w7d6-resume-demo"},
        "recursion_limit": 30,
    }
    planner: Planner | None = None
    checkpoints: list[str] = []

    with tracing.tracer.start_as_current_span("agent.run", attributes={"agent.task": task}) as root:
        graph = build_graph(
            ctx=tracing, tools=toolset, planner=planner,
            checkpointer=saver, interrupt_before=["tools"],
        )
        graph.invoke(initial, config=config)
        for _ in range(max_resumes):
            snapshot = graph.get_state(config)
            next_nodes = tuple(snapshot.next)
            if not next_nodes:
                break
            checkpoints.append("/".join(next_nodes))
            # 故意每次重新编译一张图：状态只能来自共享的 InMemorySaver
            graph = build_graph(
                ctx=tracing, tools=toolset, planner=planner,
                checkpointer=saver, interrupt_before=["tools"],
            )
            graph.invoke(None, config=config)
        final = graph.get_state(config).values
        root.set_attribute("agent.finish_reason", final.get("finish_reason") or "max_steps")
        root.set_attribute("agent.steps", int(final["steps"]))
    return _to_result(final), tracing, checkpoints
