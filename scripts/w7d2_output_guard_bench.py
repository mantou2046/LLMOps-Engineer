"""W7 D2 输出护栏真实检查器量化评估：误拦率、延迟、token 用量。

零侵入：用 monkeypatch 包一层 llm.chat 记录 usage 与墙钟耗时，判定仍走 production 路径。
结果写 evals/out/output_guard_bench.json（本机产物，不入库）；
结论与口径写在 evals/OUTPUT_GUARD_BENCH.md。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("OBSERVABILITY", "off")

CASES = ROOT / "evals" / "guardrails" / "output_judge_cases.jsonl"
OUT = ROOT / "evals" / "out" / "output_guard_bench.json"


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    import llm
    import output_guard

    if llm.LLM_MODE != "openai":
        raise SystemExit("需要 LLM_MODE=openai 才能量真实判定；当前是 mock 或未配置")

    usage_log: list[dict] = []
    latency_log: list[float] = []
    raw_log: list[dict] = []
    real_chat = llm.chat

    def tracking_chat(**kwargs):
        started = time.perf_counter()
        outcome = real_chat(**kwargs)
        latency_log.append(time.perf_counter() - started)
        usage_log.append(dict(outcome.usage or {}))
        raw_log.append(
            {
                "model": outcome.model,
                "tier": outcome.tier,
                "content": str(outcome.content)[:200],
            }
        )
        return outcome

    llm.chat = tracking_chat
    cases = [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = []
    try:
        for case in cases:
            before = len(raw_log)
            try:
                if case["kind"] == "toxicity":
                    actual = output_guard.judge_toxicity(case["answer"])
                else:
                    supported = output_guard.judge_grounding(
                        case["answer"], case["context"]
                    )
                    actual = "supported" if supported else "unsupported"
                error = ""
            except Exception as exc:
                actual = "error"
                error = f"{type(exc).__name__}: {str(exc)[:120]}"
            rows.append(
                {
                    "id": case["id"],
                    "kind": case["kind"],
                    "expected": case["expected"],
                    "actual": actual,
                    "ok": actual == case["expected"],
                    "error": error,
                    "note": case["note"],
                    "raw": raw_log[-1] if len(raw_log) > before else None,
                }
            )
            # 批量调用会撞上游限流/超时，留一点间隔（W6 D5 的 burst 体检教训）。
            time.sleep(0.5)
    finally:
        llm.chat = real_chat

    def group(kind: str, expected: str) -> dict:
        sub = [
            r
            for r in rows
            if r["kind"] == kind and r["expected"] == expected
        ]
        valid = [r for r in sub if r["actual"] != "error"]
        return {
            "total": len(sub),
            "valid": len(valid),
            "errors": len(sub) - len(valid),
            "wrong": [r["id"] for r in valid if not r["ok"]],
        }

    normal = group("toxicity", "allow")
    attack = group("toxicity", "block")
    grounded = [r for r in rows if r["kind"] == "grounding"]
    grounded_valid = [r for r in grounded if r["actual"] != "error"]
    grounded_wrong = [r["id"] for r in grounded_valid if not r["ok"]]
    errors = [r for r in rows if r["actual"] == "error"]

    def rate(num: int, den: int) -> str:
        return f"{num / den * 100:.1f}%" if den else "n/a"

    print("=== 毒性判定 ===")
    print(
        f"正常样本（期望 allow）：{normal['total']} 条，有效 {normal['valid']}，"
        f"误拦 {len(normal['wrong'])} → FPR {rate(len(normal['wrong']), normal['valid'])} {normal['wrong']}"
    )
    print(
        f"应拦样本（期望 block）：{attack['total']} 条，有效 {attack['valid']}，"
        f"漏拦 {len(attack['wrong'])} → FNR {rate(len(attack['wrong']), attack['valid'])} {attack['wrong']}"
    )
    print("=== 资料依据判定 ===")
    print(
        f"{len(grounded)} 条，有效 {len(grounded_valid)}，判错 {len(grounded_wrong)} → "
        f"准确率 {rate(len(grounded_valid) - len(grounded_wrong), len(grounded_valid))} {grounded_wrong}"
    )
    print("=== 判定失败（生产里会 fail-closed 兜底）===")
    print(f"error {len(errors)} 条")
    for row in errors:
        print(f"  {row['id']} {row['error']}")
        print(f"    原始返回: {row['raw']}")
    print("=== 单次判定延迟（llm.chat 墙钟，含网络与网关）===")
    if latency_log:
        print(
            f"n={len(latency_log)}  P50={statistics.median(latency_log):.3f}s  "
            f"P95={_p95(latency_log):.3f}s  min={min(latency_log):.3f}s  "
            f"max={max(latency_log):.3f}s"
        )
    print("=== token 用量 ===")

    def total(key: str) -> int:
        return sum(u.get(key) or 0 for u in usage_log)

    prompt_tokens = total("prompt_tokens")
    completion_tokens = total("completion_tokens")
    all_tokens = total("total_tokens")
    calls = max(1, len(usage_log))
    print(
        f"调用 {len(usage_log)} 次：prompt {prompt_tokens}（均值 {prompt_tokens / calls:.1f}）、"
        f"completion {completion_tokens}（均值 {completion_tokens / calls:.1f}）、"
        f"total {all_tokens}（均值 {all_tokens / calls:.1f}）"
    )
    print(
        f"一次结构化回答最多 2 次判定 → 额外约 {2 * all_tokens / calls:.0f} tokens（按均值估算）"
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "cases": len(cases),
                "rows": rows,
                "latency": {
                    "n": len(latency_log),
                    "p50": statistics.median(latency_log) if latency_log else None,
                    "p95": _p95(latency_log) if latency_log else None,
                    "min": min(latency_log) if latency_log else None,
                    "max": max(latency_log) if latency_log else None,
                },
                "usage": {
                    "calls": len(usage_log),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": all_tokens,
                },
                "summary": {
                    "normal": normal,
                    "attack": attack,
                    "grounding": {
                        "total": len(grounded),
                        "valid": len(grounded_valid),
                        "wrong": grounded_wrong,
                    },
                    "errors": [r["id"] for r in errors],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n明细已写 {OUT.relative_to(ROOT)}（本机产物，不入库）")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
