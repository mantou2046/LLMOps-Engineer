#!/usr/bin/env python
"""W2 D10 · 本地推理通道（Ollama）快速压测。

只测本地通道的延迟与吞吐，作为 DEC-019 的量化支撑。
直接打 Ollama 的 OpenAI 兼容端点，不经过 app（避免引入 app 侧开销）。

对比数据说明：本机无 GPU，本地通道的数字会显著慢于托管 API —— 这正是要记录的事实
（不是「谁快」，而是「量化差距，说明生产为什么要 GPU」）。

用法：
  python scripts/bench_ollama.py --n 10 --concurrency 1
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen2.5:0.5b"


def one(max_tokens: int, timeout: float) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "用一句话解释什么是连续批处理。"}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    dt = (time.perf_counter() - t0) * 1000
    usage = payload.get("usage", {}) or {}
    return {"latency_ms": dt, "completion_tokens": usage.get("completion_tokens", 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()

    # 预热：首次调用会把模型加载进内存，不计入统计
    one(8, args.timeout)

    t0 = time.perf_counter()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(one, args.max_tokens, args.timeout) for _ in range(args.n)]
        for f in as_completed(futs):
            rows.append(f.result())
    wall = time.perf_counter() - t0

    lat = sorted(r["latency_ms"] for r in rows)
    toks = sum(r["completion_tokens"] for r in rows)

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    print(f"n={args.n} concurrency={args.concurrency} max_tokens={args.max_tokens}")
    print(f"p50={pct(.5):.0f}ms  p90={pct(.9):.0f}ms  p99={pct(.99):.0f}ms")
    print(f"wall={wall:.1f}s  RPS={args.n / wall:.2f}  tok/s={toks / wall:.1f}")
    print(f"mean={statistics.mean(lat):.0f}ms  total_completion_tokens={toks}")


if __name__ == "__main__":
    main()
