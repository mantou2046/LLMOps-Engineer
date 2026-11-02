"""W5 D3 · 网关负载发生器（四类流量 + 客户端侧 TTFT 基准）

为什么需要它 —— 和 D1 的 loadgen.py 同一个理由：
  验证「指标准不准」，必须有一个**独立于被测系统的**客户端视角基准。
  只信 Prometheus 算出来的数不行，因为那正是被检验的对象。

  尤其 TTFT：网关说 0.30s，客户端说 0.31s，才叫「对上了」。
  只有网关的数，你无法区分「指标算得对」和「指标和上游串通好一起骗我」。

四类流量各解决一个口径问题（对应手册步骤 6）：

  A 非流式 × 4 到便宜档      → 请求数 / token / 成本，全都不该被缓存污染
  B 非流式 × 4 到贵档        → 成本按档位拆分：单价差 10 倍，面板上要看得出来
  C 流式 × 3 到便宜档        → **TTFT 的唯一来源**（非流式不发 TTFT 指标）
  D 打 3 次打不通的档位      → 错误率：网关重试后仍失败，客户端拿到 5xx
  E 同 body 连打 3 次        → 缓存命中：1 miss + 2 hit，看 token 涨而成本不涨

⚠️ 每个请求的正文都带唯一序号（nonce），否则 LiteLLM 的精确匹配缓存会把
   后面几个直接吃掉 —— 实测：同 body 第二次只要 12ms，而 miss 是 500ms。
   那是缓存该有的样子，但它会让「请求数 / 成本」全部失真。

只用标准库：验证脚本不该引入新依赖，否则「验证」本身成了故障源。

用法（仓库根，终端 B；网关必须已在 4001 上跑）：
  D:\\python314\\python.exe scripts/gateway_load.py
  D:\\python314\\python.exe scripts/gateway_load.py --base-url http://127.0.0.1:4001
  D:\\python314\\python.exe scripts/gateway_load.py --key sk-xxx     # 不给就读 .env
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import time
import urllib.error
import urllib.request


def _opener() -> urllib.request.OpenerDirector:
    """不带代理的 opener —— 本机 HTTP_PROXY 会把连 127.0.0.1 的请求也转出去。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _master_key(cli_key: str) -> str:
    """优先用命令行给的；否则从仓库根 .env 读 LITELLM_MASTER_KEY。"""
    if cli_key:
        return cli_key
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _post(
    opener,
    url: str,
    key: str,
    model: str,
    text: str,
    *,
    stream: bool,
    timeout: float = 60.0,
) -> dict:
    """发一次 /v1/chat/completions，返回 {code, ttft, total}（秒）。

    TTFT 在客户端侧量：流式时记「读到第一条带 content 的 data 行」的时刻。
    这是跟网关指标对账的唯一办法 —— 两边独立量，数值接近才算指标可信。
    """
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": text}],
            "stream": stream,
        }
    ).encode()
    req = urllib.request.Request(
        url + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.perf_counter()
    ttft = float("nan")
    try:
        with opener.open(req, timeout=timeout) as resp:
            if not stream:
                resp.read()
                return {"code": resp.status, "ttft": float("nan"), "total": time.perf_counter() - t0}
            # 流式：逐行读，第一行有 content 的时刻 = 客户端看到的 TTFT
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                if ttft != ttft:  # nan 判定：还没记过
                    try:
                        frame = json.loads(body)
                        if frame.get("choices"):
                            delta = frame["choices"][0].get("delta") or {}
                            if delta.get("content"):
                                ttft = time.perf_counter() - t0
                    except (ValueError, KeyError):
                        pass
            return {"code": resp.status, "ttft": ttft, "total": time.perf_counter() - t0}
    except urllib.error.HTTPError as exc:
        exc.read()
        return {"code": exc.code, "ttft": float("nan"), "total": time.perf_counter() - t0}


def _ms(x: float) -> str:
    return "   —   " if x != x else f"{x * 1000:8.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="W5 D3 网关负载发生器")
    ap.add_argument("--base-url", default="http://127.0.0.1:4001")
    ap.add_argument("--key", default="", help="LITELLM_MASTER_KEY；不给则读 .env")
    ap.add_argument("--run-id", default="", help="本次批次的 nonce 前缀，避免与上一轮缓存撞车")
    args = ap.parse_args()

    key = _master_key(args.key)
    if not key:
        print("错误：没有 master key。用 --key 传，或确认仓库根 .env 里有 LITELLM_MASTER_KEY")
        return 2
    run_id = args.run_id or str(int(time.time()))[-6:]
    opener = _opener()
    base = args.base_url.rstrip("/")

    rows: list[tuple[str, str, dict]] = []

    def batch(name: str, model: str, n: int, *, stream: bool, same_body: bool = False) -> None:
        for i in range(1, n + 1):
            # same_body=True 时用同一段正文 → 第 2 次起命中缓存
            text = f"口径验证 {run_id} 固定正文" if same_body else f"口径验证 {run_id} 第 {name} 组第 {i} 条"
            r = _post(opener, base, key, model, text, stream=stream)
            rows.append((name, model, r))

    batch("A", "local-cheap", 4, stream=False)
    batch("B", "local-primary", 4, stream=False)
    batch("C", "local-cheap", 3, stream=True)
    batch("D", "local-dead", 3, stream=False)
    batch("E", "local-cheap", 3, stream=False, same_body=True)

    print(f"\nrun-id = {run_id}   目标 = {base}\n")
    print(f"{'组':<3}{'档位':<15}{'#':>3}{'状态':>6}{'TTFT(ms)':>11}{'总耗时(ms)':>12}")
    print("-" * 52)
    for name, model, r in rows:
        idx = sum(1 for n2, m2, _ in rows[: rows.index((name, model, r))] if n2 == name) + 1
        print(f"{name:<3}{model:<15}{idx:>3}{r['code']:>6}{_ms(r['ttft']):>11}{_ms(r['total']):>12}")

    # ---- 汇总：每个数字都要能在手册里手算核对 ------------------------------
    print("\n—— 汇总（对着手册步骤 7 的期望值核）——")
    for name in ("A", "B", "C", "D", "E"):
        sub = [r for n, _, r in rows if n == name]
        codes = {}
        for r in sub:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
        line = f"  {name} 组：{len(sub)} 个请求，状态码 {codes}"
        tt = [r["ttft"] for r in sub if r["ttft"] == r["ttft"]]
        if tt:
            line += f"，TTFT p50 {statistics.median(tt) * 1000:.0f}ms"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
