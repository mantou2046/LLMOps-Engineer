#!/usr/bin/env python
"""W2 D10 · 双通道延迟 / 吞吐对比压测。

对象是 app 的 `/chat`（不是直接打 vLLM）——因为要测的是「我们这条链路」的端到端表现：
  通道 A（自托管）：/chat tier=fallback（或 primary，取决于 env 把 base_url 指向哪）
  通道 B（托管/mock）：/chat tier=primary

⚠️ 本机无 GPU：vLLM CPU 模式的数字会显著慢于托管 API，这正是要记录的事实——
   不是「测出谁快」，而是「量化差距，说明为什么生产要 GPU」。

用法：
  python scripts/bench_channels.py --base http://localhost:8787 \
      --channels primary,fallback --n 10 --concurrency 1

参数：
  --base          app 服务地址（先跑 scripts/dev_app.sh 或容器）
  --channels      要测的 tier 名，逗号分隔
  --n             每通道请求数
  --concurrency   并发数（1 = 串行）
  --max-tokens    每次生成上限（控制单请求时长）
  --out           结果 markdown 落盘路径（可选）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def _one(base: str, tier: str, max_tokens: int, timeout: float) -> dict:
    """打一次 /chat，回传本次的关键指标。"""
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "用一句话解释什么是连续批处理。"}],
            "model": tier,
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            headers = resp.headers
        dt = (time.perf_counter() - t0) * 1000
        usage = payload.get("usage", {}) or {}
        return {
            "ok": True,
            "latency_ms": dt,
            "server_latency_ms": payload.get("latency_ms"),
            "attempts": payload.get("attempts"),
            "retries": payload.get("retries"),
            "degraded": payload.get("degraded"),
            "total_tokens": usage.get("total_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "model": payload.get("model"),
            "tier": payload.get("tier"),
            "hdr_retries": headers.get("X-Upstream-Retries"),
            "hdr_degraded": headers.get("X-LLM-Degraded"),
        }
    except urllib.error.HTTPError as exc:
        dt = (time.perf_counter() - t0) * 1000
        return {"ok": False, "latency_ms": dt, "error": f"HTTP {exc.code}: {exc.read()[:200]!r}"}
    except Exception as exc:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        return {"ok": False, "latency_ms": dt, "error": f"{type(exc).__name__}: {exc}"}


def bench_channel(base: str, tier: str, n: int, concurrency: int, max_tokens: int, timeout: float) -> dict:
    results: list[dict] = []
    wall0 = time.perf_counter()
    if concurrency <= 1:
        for _ in range(n):
            results.append(_one(base, tier, max_tokens, timeout))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(_one, base, tier, max_tokens, timeout) for _ in range(n)]
            for f in as_completed(futs):
                results.append(f.result())
    wall = time.perf_counter() - wall0

    ok = [r for r in results if r["ok"]]
    lat = sorted(r["latency_ms"] for r in ok)
    total_tokens = sum(r.get("total_tokens", 0) for r in ok)
    comp_tokens = sum(r.get("completion_tokens", 0) for r in ok)

    def pct(p: float) -> float | None:
        if not lat:
            return None
        idx = min(len(lat) - 1, int(round((p / 100) * (len(lat) - 1))))
        return round(lat[idx], 1)

    return {
        "tier": tier,
        "n": n,
        "concurrency": concurrency,
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "wall_s": round(wall, 2),
        "lat_p50_ms": pct(50),
        "lat_p90_ms": pct(90),
        "lat_p99_ms": pct(99),
        "lat_mean_ms": round(statistics.mean(lat), 1) if lat else None,
        "rps": round(len(ok) / wall, 2) if wall else None,
        "output_tokens_per_s": round(comp_tokens / wall, 2) if wall else None,
        "total_tokens": total_tokens,
        "degraded_count": sum(1 for r in ok if r.get("degraded")),
        "sample_errors": [r.get("error") for r in results if not r["ok"]][:3],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8787")
    ap.add_argument("--channels", default="primary,fallback")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    rows = []
    for ch in channels:
        print(f"== 压测通道 {ch}（n={args.n} 并发={args.concurrency}）...", flush=True)
        row = bench_channel(args.base, ch, args.n, args.concurrency, args.max_tokens, args.timeout)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2))

    # 输出 markdown 对比表
    lines = [
        "| 通道 | 成功/总 | p50(ms) | p90(ms) | p99(ms) | 均值(ms) | RPS | 输出tok/s | 降级 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['tier']} | {r['ok']}/{r['n']} | {r['lat_p50_ms']} | {r['lat_p90_ms']} | "
            f"{r['lat_p99_ms']} | {r['lat_mean_ms']} | {r['rps']} | {r['output_tokens_per_s']} | {r['degraded_count']} |"
        )
    md = "\n".join(lines)
    print("\n" + md)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(md + "\n", encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
