"""W5 D1 · 负载发生器 + 客户端侧耗时记录

为什么需要它：
  验证「监控数字准不准」，必须有一个**独立的、客户端视角的**基准。
  只信 Prometheus 自己算出来的 P95 是不行的 —— 那是桶里插值出来的，
  和真实分位数可能差一个量级（实测见 W5 D1 手册步骤 5）。
  本脚本用 urllib 直接发请求，用 perf_counter 记每次耗时，
  自己算 p50/p95/max，再跟 PromQL 的结果对照。

只用标准库：不在验证脚本里引入额外依赖，否则「验证」本身也会成为故障源。

用法（在仓库根）：
  D:\\python314\\python.exe scripts/loadgen.py                    # 默认 20 轮
  D:\\python314\\python.exe scripts/loadgen.py --rounds 40        # 压 40 轮
  D:\\python314\\python.exe scripts/loadgen.py --only /health     # 只打一个端点
  （Git Bash 下写成 --only=health 或 --only ./health 最稳；
   脚本内部已防御 MSYS 把 /health 转成 Windows 路径的坑）

⚠️ 本机代理会拦 127.0.0.1，脚本里已显式绕过（见 _opener）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from collections import defaultdict
from typing import Any


def _opener() -> urllib.request.OpenerDirector:
    """不带任何代理的 opener。

    ⚠️ urllib 默认会读 HTTP_PROXY 环境变量。本机代理（127.0.0.1:6665）
       会把连本地 8000 的请求也转发出去，结果是超时或 502 ——
       看起来像「服务挂了」。空 ProxyHandler() 是关掉它的最直接写法。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _hit(opener, url: str, *, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, float]:
    """发一次请求，返回 (状态码, 耗时秒)。4xx/5xx 不抛异常 —— 错误率本身是要测的东西。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
            return resp.status, time.perf_counter() - t0
    except urllib.error.HTTPError as exc:  # 404 / 502 都算「有响应」
        exc.read()
        return exc.code, time.perf_counter() - t0


def _pct(values: list[float], q: float) -> float:
    """最近秩法分位数（小样本下比线性插值更接近「真实看到的值」）。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def main() -> int:
    ap = argparse.ArgumentParser(description="W5 D1 负载发生器")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--rounds", type=int, default=20, help="每个端点的轮数")
    ap.add_argument("--only", default="", help="只打这个路径（如 /health）")
    args = ap.parse_args()

    # 每轮打什么：health/ready 是探针（快），work 是可控 CPU 负载（慢），
    # chat 走 mock 上游（验证「业务端点也被数到」）。
    plan: list[tuple[str, dict | None]] = [
        ("/health", None),
        ("/ready", None),
        ("/work?seconds=0.2", None),
        ("/chat", {"messages": [{"role": "user", "content": "用一句话说明什么是监控"}], "max_tokens": 32}),
    ]
    if args.only:
        # ⚠️ Git Bash（MSYS）会把 /health 这类参数自动转成 Windows 路径
        # （实测变成 D:/Program Files/.../PortableGit/health），精确比对会落空。
        # 防御写法：取末段名字再比，/health、health、D:/.../health 都能命中。
        key = args.only.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        plan = [(p, d) for p, d in plan if p.split("?")[0].lstrip("/") == key]
    if not plan:
        print(f"错误：没有端点匹配 --only={args.only!r}，不发任何请求。"
              f"可选：/health /ready /work /chat")
        return 2

    opener = _opener()
    lat: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    t_all = time.perf_counter()

    for _ in range(args.rounds):
        for path, payload in plan:
            code, dt = _hit(opener, args.base_url + path, payload=payload)
            key = path.split("?")[0]
            lat[key].append(dt)
            codes[key][code] += 1

    total = time.perf_counter() - t_all
    print(f"\n共 {sum(len(v) for v in lat.values())} 个请求，总耗时 {total:.1f}s\n")
    print(f"{'端点':<10}{'次数':>6}{'p50(ms)':>10}{'p95(ms)':>10}{'max(ms)':>10}  状态码")
    for key in ("/health", "/ready", "/work", "/chat"):
        if key not in lat:
            continue
        v = [x * 1000 for x in lat[key]]
        print(
            f"{key:<10}{len(v):>6}{_pct(v, 0.50):>10.1f}{_pct(v, 0.95):>10.1f}{_pct(v, 1.0):>10.1f}"
            f"  {dict(codes[key])}"
        )
    print(f"\n全部请求 p50 = {_pct([x*1000 for v in lat.values() for x in v], 0.5):.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
