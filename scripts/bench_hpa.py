#!/usr/bin/env python3
"""HPA 压测脚本（W2 D11）—— 纯标准库，多线程打 /work 烧 CPU。

为什么要自己写（而不是 hey / k6）：
  · 本机没装 hey/ab/k6，装一个只为压一次不划算；
  · 真正要压的是**应用自己的 /work CPU 端点**，行为单一（空转 N 秒），
    标准库 + 线程池足够，还能顺手把「延迟分位 / 成功率」一起统计出来。

⚠️ 为什么用 /work 而不是 /chat：
  HPA 的指标是 **CPU 利用率 = 实际用量 ÷ requests**。mock 模式的 /chat 主要是
  等 sleep，CPU 几乎不动，压了也扩不了容。/work 在容器内空转烧 CPU，才是
  HPA 演示的「可控负载源」（dev 的 ENABLE_LOAD_ENDPOINT=1 就是为它开的）。

用法：
  python scripts/bench_hpa.py --url http://127.0.0.1:18080/work?seconds=0.2 \
      --concurrency 12 --duration 120
  # 端口转发：kubectl port-forward -n dev svc/llmops-dev-llmops-chart 18080:80
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def worker(url: str, timeout: float, stop: threading.Event,
           results: list[tuple[float, bool]], lock: threading.Lock) -> None:
    """持续打 url 直到 stop 置位，每次记录 (耗时秒, 是否成功)。"""
    while not stop.is_set():
        t0 = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                resp.read()
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, TimeoutError):
            ok = False
        dt = time.perf_counter() - t0
        with lock:
            results.append((dt, ok))


def pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def main() -> int:
    ap = argparse.ArgumentParser(description="HPA 压测（多线程打 /work）")
    ap.add_argument("--url", required=True, help="目标 URL（含 /work?seconds=N）")
    ap.add_argument("--concurrency", type=int, default=10, help="并发线程数")
    ap.add_argument("--duration", type=float, default=60.0, help="持续秒数")
    ap.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    ap.add_argument("--warmup", type=float, default=2.0, help="预热秒数（不计入统计）")
    args = ap.parse_args()

    # ---- 预热：先探一次，确认端点真的可达（404 说明 ENABLE_LOAD_ENDPOINT 没开）----
    try:
        with urllib.request.urlopen(args.url, timeout=args.timeout) as r:
            r.read()
            print(f"[warmup] {args.url} -> HTTP {r.status} OK", flush=True)
    except urllib.error.HTTPError as e:
        print(f"[warmup] 端点返回 HTTP {e.code} —— dev 的 ENABLE_LOAD_ENDPOINT=1 开了吗？",
              file=sys.stderr, flush=True)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] 无法连接 {args.url}: {e}", file=sys.stderr, flush=True)
        return 2

    if args.warmup > 0:
        time.sleep(args.warmup)

    stop = threading.Event()
    lock = threading.Lock()
    results: list[tuple[float, bool]] = []
    threads = [
        threading.Thread(target=worker, args=(args.url, args.timeout, stop, results, lock),
                         daemon=True)
        for _ in range(args.concurrency)
    ]

    print(f"[run] {args.concurrency} 线程 × {args.duration:.0f}s -> {args.url}", flush=True)
    t_start = time.perf_counter()
    for t in threads:
        t.start()

    # 分阶段打印，方便对照 HPA 的扩缩时间线
    step = max(5, int(args.duration / 6))
    elapsed = 0.0
    while elapsed < args.duration:
        time.sleep(min(step, args.duration - elapsed))
        elapsed = time.perf_counter() - t_start
        with lock:
            n = len(results)
        print(f"  t={elapsed:5.0f}s  累计请求 {n}", flush=True)

    stop.set()
    for t in threads:
        t.join(timeout=5)
    wall = time.perf_counter() - t_start

    durs = sorted(d for d, ok in results if ok)
    ok_n = sum(1 for _, ok in results if ok)
    total = len(results)
    print("\n===== 压测结果 =====")
    print(f"总请求   : {total}")
    print(f"成功     : {ok_n}  ({100.0 * ok_n / total:.1f}%)" if total else "成功: 0")
    print(f"墙钟     : {wall:.1f}s  → RPS {total / wall:.2f}")
    if durs:
        print(f"延迟 p50 : {statistics.median(durs) * 1000:.0f} ms")
        print(f"延迟 p90 : {pct(durs, 90) * 1000:.0f} ms")
        print(f"延迟 p99 : {pct(durs, 99) * 1000:.0f} ms")
        print(f"延迟 max : {durs[-1] * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
