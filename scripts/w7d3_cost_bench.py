"""W7 D3 · 成本控制三件套离线压测：固定题集 + 四组同条件对照。

⚠️ 这是**离线仿真**，不是线上流量，也不调用真实模型：
  · 唯一变的是两个开关（精确缓存 / 规则分流）；题集、查询序列、Token 估算、
    单价表四组完全相同。这样「下降 X%」才是可归因的，不是拿两批流量硬比。
  · Token 用量按题长确定性估算（与档位无关）—— 故意不让「便宜档回答更短」
    混进省钱结论里。cost units 是演示点数，不是钱。
  · 单价与其他未计价成本项见 `app/cost.py` 文件头与 `evals/COST_BENCH.md`。

四组：
  A 基线        全 primary、无缓存
  B 只开分流    用真实 app/routing.py 判档、无缓存
  C 只开缓存    全 primary、用真实 app/cost.ExactCache
  D 两件都开    分流 + 精确缓存（任务卡要的「改动后」）

用法（仓库根目录）：
  D:\\python314\\python.exe scripts\\w7d3_cost_bench.py
"""

from __future__ import annotations

import json
import random
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import cost  # noqa: E402
import routing  # noqa: E402

CASES = ROOT / "evals" / "cost" / "queries.jsonl"
OUT = ROOT / "evals" / "out" / "w7d3" / "cost_bench.json"
N_QUERIES = 1000
SEED = 20261118
HIGH_FREQ_IDS = {"C01", "C02", "C03", "C04", "C05", "C16", "C17", "C18"}


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


REPEAT_SHARE = 0.30


def build_sequence(cases: list[dict], n: int = N_QUERIES) -> list[dict]:
    """固定种子生成 1000 次查询：30% 字面重复 + 70% 只出现一次的唯一题。

    ⚠️ 这一条是刻意设计的，别改成「20 条题抽 1000 次」——那样首轮命中率
       会虚高到 98%，因为题集里只有 20 种 key。真实日志里大多数问题只出现
       一次，精确缓存只对**一模一样的重复请求**有效（W4 D5 已实测），
       所以命中率上限就等于重复率。给唯一题加工单号后缀模拟这个分布。
    """
    rng = random.Random(SEED)
    hot = [c for c in cases if c["id"] in HIGH_FREQ_IDS]
    sequence: list[dict] = []
    for i in range(n):
        if rng.random() < REPEAT_SHARE:
            sequence.append(dict(rng.choice(hot)))
        else:
            base = rng.choice(cases)
            sequence.append(
                {**base, "text": f"{base['text']}（第 {i} 号工单）", "unique": True}
            )
    return sequence


def simulate_usage(text: str) -> dict:
    """确定性的 Token 估算：只跟题长有关，跟档位无关（公平比较的前提）。"""
    prompt = max(1, len(text) // 3) + 24  # 24 = 系统提示词的估算开销
    completion = 120 + zlib.crc32(text.encode("utf-8")) % 41
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "estimated": True,
    }


def run_scenario(
    name: str,
    sequence: list[dict],
    *,
    use_routing: bool,
    use_cache: bool,
) -> dict:
    cache = cost.ExactCache()
    ledger = cost.CostLedger()
    tier_counts: dict[str, int] = {"primary": 0, "cheap": 0}

    for case in sequence:
        messages = [{"role": "user", "content": case["text"]}]
        decision = routing.decide(messages, structured=False)
        tier = decision.tier if use_routing else "primary"
        tier_counts[tier] += 1

        ok, reason = cost.cacheable(messages, stream=False, temperature=None)
        hit = False
        if use_cache and ok:
            key = cost.cache_key(
                messages,
                tier=tier,
                structured=False,
                temperature=None,
                max_tokens=1024,
            )
            hit = cache.get(key) is not None
            if not hit:
                cache.put(key, {"content": "cached-answer-placeholder"})
        elif use_cache:
            cache.skip(reason)

        ledger.record(
            tier=tier,
            usage=simulate_usage(case["text"]),
            cache_hit=hit,
            query_id=case["id"],
        )

    report = ledger.weekly_report()
    report.update(
        {
            "scenario": name,
            "routing_enabled": use_routing,
            "cache_enabled": use_cache,
            "tier_counts": tier_counts,
            "cheap_share": round(tier_counts["cheap"] / len(sequence), 4),
            "cache_stats": cache.stats.as_dict(),
        }
    )
    return report


def routing_agreement(cases: list[dict]) -> dict:
    """顺手量一下规则分流器跟题集标注的一致率（真实 routing.decide，不 mock）。"""
    mismatches = []
    for c in cases:
        decision = routing.decide(
            [{"role": "user", "content": c["text"]}], structured=False
        )
        expected = "primary" if c["kind"] == "complex" else "cheap"
        if decision.tier != expected:
            mismatches.append(
                {
                    "id": c["id"],
                    "kind": c["kind"],
                    "expected": expected,
                    "actual": decision.tier,
                    "detail": decision.detail,
                }
            )
    return {
        "total": len(cases),
        "agreement": round((len(cases) - len(mismatches)) / len(cases), 4),
        "mismatches": mismatches,
    }


def main() -> None:
    cases = load_cases()
    sequence = build_sequence(cases)
    scenarios = [
        run_scenario("A 基线（全 primary、无缓存）", sequence, use_routing=False, use_cache=False),
        run_scenario("B 只开分流", sequence, use_routing=True, use_cache=False),
        run_scenario("C 只开精确缓存", sequence, use_routing=False, use_cache=True),
        run_scenario("D 分流 + 精确缓存", sequence, use_routing=True, use_cache=True),
    ]
    baseline = scenarios[0]["per_1000_cost_units"]
    for s in scenarios:
        s["reduction_vs_baseline"] = round(
            (baseline - s["per_1000_cost_units"]) / baseline, 4
        )

    result = {
        "seed": SEED,
        "queries_per_scenario": N_QUERIES,
        "repeat_share": REPEAT_SHARE,
        "distinct_case_texts": len({c["text"] for c in cases}),
        "cases": len(cases),
        "pricing": cost.COST_UNITS_PER_1K,
        "unpriced_items": [dict(x) for x in cost.UNPRICED_ITEMS],
        "scenarios": scenarios,
        "routing_agreement": routing_agreement(cases),
        "caveats": [
            "离线仿真，未调用真实模型；Token 按题长确定性估算，estimated=True",
            f"查询序列含 {REPEAT_SHARE:.0%} 字面重复题，其余是带唯一工单号的一次性问题",
            "精确缓存的命中率上限≈重复率；同义不同字面的题不命中",
            "cost units 是演示点数、不是货币；primary:cheap = 10:1 沿用 W5 D3 实测比例",
            "四组共用同一查询序列与单价表，只有开关不同",
            "向量库 / rerank / 缓存服务 / 网络出流量没有真实单价，未计入",
            "流式与多轮请求不进缓存；mock 模式的 usage 本身也是估算值",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("场景                          每千次成本点数  每千次Token  命中率  cheap占比  相对基线")
    for s in scenarios:
        print(
            f"{s['scenario']:<28} {s['per_1000_cost_units']:>12.1f} "
            f"{s['per_1000_tokens']:>12.1f} {s['cache_hit_rate']:>8.1%} "
            f"{s['cheap_share']:>9.1%} {s['reduction_vs_baseline']:>9.1%}"
        )
    ra = result["routing_agreement"]
    mismatch_n = len(ra["mismatches"])
    print(f"\n分流器与标注一致率：{ra['agreement']:.1%}（{ra['total']} 条，不符 {mismatch_n} 条）")
    for m in ra["mismatches"]:
        print(f"  {m['id']} {m['kind']} 期望 {m['expected']} 实际 {m['actual']}｜{m['detail']}")
    print("\n未计入的成本项：" + "、".join(x["item"] for x in cost.UNPRICED_ITEMS))
    print(f"明细：{OUT.relative_to(ROOT)}")

    a = scenarios[0]["per_1000_cost_units"]
    assert scenarios[3]["per_1000_cost_units"] < a, "组合场景必须比基线便宜"
    assert scenarios[1]["per_1000_cost_units"] < a, "分流必须比基线便宜"
    assert scenarios[2]["per_1000_cost_units"] < a, "缓存必须比基线便宜"
    assert scenarios[3]["cache_hit_rate"] > 0, "缓存场景必须有命中"
    print("\n断言 4/4 通过：两件单开都省钱、组合更省、缓存确有命中。")


if __name__ == "__main__":
    main()
