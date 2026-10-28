"""W4 D6 · 分流探针：路由命中率（离线）+ 端到端冒烟（在线）

两个模式，别混着看：

  **离线（--offline）—— 判「分流规则」本身，不受上游死活影响**
    python evals/route_probe.py --offline
    纯函数，不联网、不花 token、毫秒级跑完 24 条。
    回答「路由命中率」：便宜档占多少、与期望不符的是哪几条、为什么。
    ⚠️ 这才是「路由命中率」的正规测法 —— 路由是**应用的属性**，
       上游 429 / 模型下架都不该影响这个数字。

  **在线（默认）—— 判「链路通不通」**
    python evals/route_probe.py                # 6 条精选 × 2 轮（默认）
    python evals/route_probe.py --all          # 24 条全跑（上游不稳时别用）
    真发 HTTP 到 app:8000，记录 tier / 耗时 / tokens / 落点。
    ⚠️ 上游是免费池，随时 429；某条慢到几十秒、或 model-group 变成 backup，
       都是**上游在抖**，不是分流判错了 —— 判据看 X-Route-Tier，不看耗时。

缓存命中率不在本脚本里：应用层看不到网关的 x-litellm-cache-key
（openai SDK 不暴露响应头），只能给「耗时骤降」这种间接判据 ——
而上游一抖，间接判据立刻失效。硬证据走 curl 直连网关看响应 id（手册步骤 4）。

输出：控制台表格 + evals/out/route_probe_<模式>_<时间戳>.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import routing  # noqa: E402  （纯标准库，import 无副作用）

# ---- 24 条题面：12 简单 + 12 复杂 -----------------------------------------
# 选材原则：全部取自作品集真实场景（资金/财务制度问答），不造无意义的测试串。
# 「期望档位」是人工标注的**基准答案**，路由判错就是判错，不许回头改标注来凑分。
QUESTIONS: list[tuple[str, str, str]] = [
    # 简单题：短、只要一个事实或一个定义
    ("s1", "cheap", "银行回单是什么？"),
    ("s2", "cheap", "资金池的零余额账户怎么填开户信息？"),
    ("s3", "cheap", "银企直联在哪配置？"),
    ("s4", "cheap", "票据贴现的定义是什么？"),
    ("s5", "cheap", "到账通知一般包含多少字段？"),
    ("s6", "cheap", "收款结算单叫什么？"),
    ("s7", "cheap", "今天上证指数收盘多少点？"),
    ("s8", "cheap", "银行回单要盖几个章？"),
    ("s9", "cheap", "总账凭证在哪打印？"),
    ("s10", "cheap", "银行账户余额是什么？"),
    ("s11", "cheap", "银企直联的日终对账叫什么？"),
    ("s12", "cheap", "票据到期日怎么填？"),
    # 复杂题：要对比 / 要推理 / 要引用材料
    ("c1", "primary", "银行回单和到账通知有什么区别？"),
    ("c2", "primary", "票据贴现和票据质押融资有什么区别？"),
    ("c3", "primary", "为什么银行账户余额和账面余额会对不上？请分析常见原因。"),
    ("c4", "primary", "银企直联的日终对账一般分哪几步？请梳理并说明每步的风险点。"),
    ("c5", "primary", "收款结算单和总账凭证之间是什么关系？请对比两者的记账时点。"),
    (
        "c6",
        "primary",
        "根据以下材料回答：某集团 2026 年 10 月的资金归集方案要求「下属成员单位每日 17:00 前"
        "将超出限额的资金自动上划至集团主账户，主账户按周下拨备用金」。请分析这套方案在"
        "月末最后一天可能出现的资金缺口风险，并给出两种优化方案及其取舍。",
    ),
    (
        "c7",
        "primary",
        "根据以下材料回答：制度原文第 12 条规定「单笔付款超过 500 万元的，须经财务负责人"
        "与分管副总双签」。请评估该条款在子公司自行付款场景下的合规风险。",
    ),
    ("c8", "primary", "资金池归集和拨付的账务处理流程是什么？请梳理涉及的会计科目并说明对报表的影响。"),
    ("c9", "primary", "我们打算把子公司付款全部收归集团统一支付，请分析这个方案的风险和取舍。"),
    ("c10", "primary", "请评估现有银企直联接口的稳定性风险，并给出两种优化方案。"),
    ("c11", "primary", "请对比票据贴现和票据质押融资在资金成本上的区别，并给出选择依据。"),
    ("c12", "primary", "为什么月末资金归集容易出现缺口？请推导资金流路径并说明每一步的影响。"),
]

# 在线模式默认只跑这 6 条：3 简单 + 3 复杂。
# 为什么不跑全量：上游免费池一条可能 1~50 秒，24 条 × 2 轮足够把人耗走。
LIVE_IDS = ("s1", "s3", "s7", "c1", "c3", "c6")

# ---- 已知边界样本：**故意留着判错的**，用来标定规则的适用范围 ----------------
# 这三个不算进命中率 —— 它们的作用是提醒「规则有边界，别拿 24/24 当泛化能力」。
# 每条后面写清误判方向：**过判**（该走 cheap 却走了 primary，多花钱但不致命）
# 还是**漏判**（该走 primary 却走了 cheap，省钱但答得差）。
BOUNDARY: list[tuple[str, str, str, str]] = [
    ("b1", "cheap", "票据贴现的计算方式是什么？", "强信号词「计算」把定义题拉成 primary → 过判"),
    ("b2", "primary", "刚才那个问题再解释一遍，顺便把口径说清楚。", "单看这句无信号词 → 判 cheap；真正难度在上一轮上下文里 → 漏判"),
    ("b3", "primary", "请把到账通知的字段名逐个列出来。", "无强/弱信号词、长度不足 → 判 cheap；但要求完整枚举，轻模型容易漏字段 → 漏判"),
]


def _post(base_url: str, payload: dict, timeout: float) -> tuple[int, dict, dict, float]:
    """发一次 /chat，返回 (status, headers, body, 端到端耗时ms)。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本地固定地址
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, dict(resp.headers), body, (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw[:400]}
        return exc.code, dict(exc.headers or {}), body, (time.perf_counter() - t0) * 1000


def bench_router(rounds: int = 20000) -> float:
    """规则判档的单次耗时（µs）。纯函数、无网络、无模型 —— 这就是它的全部成本。"""
    msgs = [{"role": "user", "content": "银行回单和到账通知有什么区别？请分析记账时点。"}]
    t0 = time.perf_counter()
    for _ in range(rounds):
        routing.decide(msgs, structured=False)
    return (time.perf_counter() - t0) / rounds * 1e6


def run_offline() -> int:
    """只跑路由判定：不出网、不花 token。"""
    us = bench_router()
    print(f"规则判档耗时：{us:.1f} µs/次（20000 次取均值，纯函数无网络）\n")
    print(f"{'id':<4}{'期望':<9}{'实际':<9}{'score':>6}  {'signals':<34}{'chars':>6}  判据")
    rows = []
    for qid, expect, q in QUESTIONS:
        d = routing.decide([{"role": "user", "content": q}], structured=False)
        mark = "✅" if d.tier == expect else "⚠️"
        print(f"{qid:<4}{expect:<9}{d.tier + mark:<9}{d.score:>6}  {','.join(d.signals):<34}{d.chars:>6}  {d.detail}")
        rows.append(
            {
                "id": qid,
                "expect": expect,
                "route_tier": d.tier,
                "score": d.score,
                "signals": ",".join(d.signals),
                "chars": d.chars,
                "detail": d.detail,
                "question": q,
            }
        )
    cheap = [r for r in rows if r["route_tier"] == "cheap"]
    wrong = [r for r in rows if r["route_tier"] != r["expect"]]
    print("\n===== 汇总 =====")
    print(f"路由命中率：cheap {len(cheap)}/{len(rows)} = {len(cheap) / len(rows):.0%}"
          f"｜primary {len(rows) - len(cheap)}/{len(rows)} = {(len(rows) - len(cheap)) / len(rows):.0%}")
    print(f"与人工标注不符：{len(wrong)} 条" + (f" → {[r['id'] for r in wrong]}" if wrong else "（0 条）"))

    print("\n===== 已知边界样本（不计入上面的命中率）=====")
    print(f"{'id':<4}{'期望':<9}{'实际':<9}{'score':>6}  说明")
    for bid, expect, q, note in BOUNDARY:
        d = routing.decide([{"role": "user", "content": q}], structured=False)
        mark = "✅" if d.tier == expect else "⚠️"
        print(f"{bid:<4}{expect:<9}{d.tier + mark:<9}{d.score:>6}  {note}")

    out = REPO / "evals" / "out" / f"route_probe_offline_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"明细已落：{out}")
    return 0


def run_live(base_url: str, rounds: int, timeout: float, use_all: bool) -> int:
    picked = QUESTIONS if use_all else [q for q in QUESTIONS if q[0] in LIVE_IDS]
    rows: list[dict] = []
    for rnd in range(1, rounds + 1):
        print(f"===== 第 {rnd} 轮（{len(picked)} 条）=====")
        print(f"{'id':<4}{'期望':<9}{'实际':<9}{'score':>6}  {'signals':<24}{'耗时ms':>9}{'tokens':>8}  状态")
        for qid, expect, q in picked:
            status, headers, body, ms = _post(
                base_url,
                {"model": "auto", "messages": [{"role": "user", "content": q}], "max_tokens": 64},
                timeout,
            )
            rt = body.get("routing", {}) if isinstance(body, dict) else {}
            tier = headers.get("X-Route-Tier") or rt.get("tier") or "?"
            score = headers.get("X-Route-Score") or str(rt.get("score", ""))
            signals = headers.get("X-Route-Signals") or ",".join(rt.get("signals", []))
            usage = body.get("usage", {}) if isinstance(body, dict) else {}
            mark = "✅" if tier == expect else "⚠️"
            print(
                f"{qid:<4}{expect:<9}{tier + mark:<9}{score:>6}  {signals[:24]:<24}"
                f"{ms:>9.0f}{str(usage.get('total_tokens', '')):>8}  [{status}]"
            )
            rows.append(
                {
                    "round": rnd,
                    "id": qid,
                    "expect": expect,
                    "route_tier": tier,
                    "route_score": score,
                    "signals": signals,
                    "latency_ms": round(ms, 1),
                    "total_tokens": usage.get("total_tokens", ""),
                    "served_model": (body or {}).get("model", ""),
                    "http_status": status,
                    "route_detail": rt.get("detail", ""),
                    "request_id": (body or {}).get("request_id", ""),
                    "question": q,
                }
            )
        print()

    first = [r for r in rows if r["round"] == 1]
    cheap = [r for r in first if r["route_tier"] == "cheap"]
    print("===== 汇总 =====")
    print(f"路由命中率（在线这 {len(first)} 条）：cheap {len(cheap)}/{len(first)} = {len(cheap) / len(first):.0%}")
    print(f"与期望不符：{len([r for r in first if r['route_tier'] != r['expect']])} 条")
    lat = [r["latency_ms"] for r in rows]
    print(f"耗时：中位数 {statistics.median(lat):.0f}ms / 最小 {min(lat):.0f}ms / 最大 {max(lat):.0f}ms"
          "（上游免费池波动极大，别拿单次当基线）")
    print("⚠️ 耗时只说明上游快慢，**不说明分流对错**；分流判据是 X-Route-Tier。")
    out = REPO / "evals" / "out" / f"route_probe_live_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"明细已落：{out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只跑路由判定，不出网（推荐先跑这个）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--rounds", type=int, default=2, help="每个问题发几遍")
    ap.add_argument("--timeout", type=float, default=180.0, help="单请求超时秒数")
    ap.add_argument("--all", action="store_true", help="在线模式跑全部 24 条（上游不稳时别用）")
    args = ap.parse_args()
    if args.offline:
        return run_offline()
    return run_live(args.base_url, args.rounds, args.timeout, args.all)


if __name__ == "__main__":
    raise SystemExit(main())
