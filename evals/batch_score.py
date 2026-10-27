"""W4 D4 · 离线批量打分（LLM 版）

读一批问题 → 批量调用 LLM → 落结果表。对应 M4 4.5 的五段：
  1 读数据   读 evals/smoke_set.jsonl（可循环扩到 --n 条）
  2 备输入   取 question 字段
  3 载模型   复用 app/llm.py（启动时定档位；mock 模式无需 key）
  4 算预测   并发调用 llm.chat()
  5 写结果   带 model / tier / tokens / latency / scored_at 的 parquet

默认 **mock 模式**（LLM_MODE=mock + OBSERVABILITY=off）：无 key 也能跑，
token 是估算值（estimated=True）。加 `--real` 才沿用 .env 的真实模式（openai+langfuse）。

用法（PowerShell / Git Bash 通用，无需先设环境变量）：
  python evals/batch_score.py --n 100 --workers 8     # 批量（8 并发）
  python evals/batch_score.py --n 100 --workers 1     # 逐条（串行对照）
  python evals/batch_score.py --n 100 --workers 4 --real   # 真模型（需 .env 里配好 key）

输出：evals/out/batch_<mode>_w<workers>.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))  # app/ 下是平级绝对导入（import llm）

# ⚠️ 必须在 import llm 之前定模式：llm/observability 会在导入时读 env，
#    load_dotenv 默认不覆盖已存在的变量 → 这里先写死 mock，.env 就盖不掉。
if "--real" not in sys.argv:
    os.environ["LLM_MODE"] = "mock"
    os.environ["OBSERVABILITY"] = "off"

import llm  # noqa: E402  （路径与 env 就绪后再导入）

# 参考单价（USD / 1M tokens）。mock 的 token 是估算值，成本自然也是估算，别当真账。
PRICING = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4": (0.28, 0.42),
}
DEFAULT_PRICE = (0.14, 0.28)


def load_questions(path: Path, n: int) -> list[dict]:
    """读问题集，循环扩到 n 条（批量演示要 100 条，源集只有 10 条）。"""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"问题集为空：{path}")
    out: list[dict] = []
    i = 0
    while len(out) < n:
        r = dict(rows[i % len(rows)])
        r["_seq"] = len(out)
        out.append(r)
        i += 1
    return out


def score_one(q: dict, max_tokens: int, sim_latency_ms: int = 0) -> dict:
    """单条：调用一次 llm.chat()，成功失败都返回一行（批量跑不能因一条挂掉整批）。

    sim_latency_ms：**仅 mock 模式**下模拟真实 API 的网络延迟（sleep）。
    真实 API 自带延迟，加 --real 时忽略此项。它是为了在没有 key 时也能演示
    「并发 vs 串行」的耗时差——不是真实测量，手册里会写明。
    """
    t0 = time.perf_counter()
    base = {"seq": q["_seq"], "id": q.get("id"), "type": q.get("type"), "question": q["question"]}
    try:
        if sim_latency_ms > 0 and llm.LLM_MODE == "mock":
            time.sleep(sim_latency_ms / 1000.0)
        r = llm.chat(messages=[{"role": "user", "content": q["question"]}], max_tokens=max_tokens)
        return {
            **base,
            "answer": str(r.content)[:500],
            "model": r.model,
            "tier": r.tier,
            "mode": r.mode,
            "latency_ms": round(r.latency_ms, 2),
            "prompt_tokens": r.usage.get("prompt_tokens") or 0,
            "completion_tokens": r.usage.get("completion_tokens") or 0,
            "total_tokens": r.usage.get("total_tokens") or 0,
            "estimated": bool(r.usage.get("estimated")),
            "attempts": r.attempts,
            "retries": r.retries,
            "error": "",
            "wall_ms": round((time.perf_counter() - t0) * 1000, 2),
            "scored_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001 —— 批量里一条失败不该炸整批
        return {
            **base,
            "answer": "",
            "model": "",
            "tier": "",
            "mode": llm.LLM_MODE,
            "latency_ms": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated": None,
            "attempts": 0,
            "retries": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "wall_ms": round((time.perf_counter() - t0) * 1000, 2),
            "scored_at": datetime.now().isoformat(timespec="seconds"),
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="W4 D4 离线批量打分（LLM）")
    ap.add_argument("--input", default=str(REPO / "evals" / "smoke_set.jsonl"))
    ap.add_argument("--n", type=int, default=100, help="要打多少条（源集循环扩充）")
    ap.add_argument("--workers", type=int, default=8, help="并发数；1 = 逐条串行")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--sim-latency-ms", type=int, default=0, help="仅 mock 模式：模拟真实 API 网络延迟（毫秒），用于演示并发 vs 串行")
    ap.add_argument("--out-dir", default=str(REPO / "evals" / "out"))
    ap.add_argument("--real", action="store_true", help="用 .env 的真实模式（openai+langfuse），需 key")
    args = ap.parse_args()

    questions = load_questions(Path(args.input), args.n)
    print(f"[load] {len(questions)} 条问题（源 {Path(args.input).name}，模式 {llm.LLM_MODE}，workers={args.workers}）")

    t0 = time.perf_counter()
    results: list[dict] = []
    if args.workers <= 1:
        for q in questions:  # 逐条：串行
            results.append(score_one(q, args.max_tokens, args.sim_latency_ms))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:  # 批量：并发，worker 数就是并发上限
            futs = [ex.submit(score_one, q, args.max_tokens, args.sim_latency_ms) for q in questions]
            for f in as_completed(futs):
                results.append(f.result())
    total_s = time.perf_counter() - t0
    results.sort(key=lambda x: x["seq"])

    df = pd.DataFrame(results)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{llm.LLM_MODE}_w{args.workers}"
    out_file = out_dir / f"batch_{tag}.parquet"
    df.to_parquet(out_file, index=False)

    errs = int((df["error"].astype(str).str.len() > 0).sum())
    model0 = str(df["model"].iloc[0]) if len(df) else ""
    pin, pout = PRICING.get(model0, DEFAULT_PRICE)
    cost = (
        float(df["prompt_tokens"].sum()) * pin + float(df["completion_tokens"].sum()) * pout
    ) / 1_000_000

    print(
        json.dumps(
            {
                "n": len(df),
                "workers": args.workers,
                "errors": errs,
                "total_s": round(total_s, 2),
                "items_per_s": round(len(df) / total_s, 1),
                "ms_per_item": round(total_s * 1000 / len(df), 2),
                "total_tokens": int(df["total_tokens"].sum()),
                "est_cost_usd": round(cost, 6),
                "usd_per_1k_items": round(cost / len(df) * 1000, 6),
                "out": str(out_file),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
