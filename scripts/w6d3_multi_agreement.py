#!/usr/bin/env python3
"""三位及以上盲标员的一致性矩阵：两两一致、全体一致、多数一致，并与已标正本对照。

只报数字，不判定验收通过与否。原两方脚本 w6d3_blind_agreement.py 保持不变。
用法：./.venv-eval/Scripts/python.exe scripts/w6d3_multi_agreement.py \
        --files blind3_DSV3.json blind3_CodexV3.json blind3_MM3.json
"""
from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "evals" / "out" / "w6d3"
ITEMS = BASE / "blind_items.json"
GOLD = ROOT / "evals" / "golden_set.jsonl"


def load(name: str) -> tuple[dict, dict]:
    obj = json.loads((BASE / name).read_text(encoding="utf-8"))
    return obj.get("answers", obj), obj.get("notes", {})


def as_set(v) -> frozenset:
    if isinstance(v, list):
        return frozenset(v)
    return frozenset({str(v)})


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--files", nargs="+", required=True, help="两到多份盲标结果文件名")
    args = p.parse_args()
    if len(args.files) < 2:
        print("至少需要两份结果", file=__import__("sys").stderr)
        return 1

    items = {x["id"]: x for x in json.loads(ITEMS.read_text(encoding="utf-8"))}
    ids = sorted(items)
    gold = {
        r["id"]: r for r in
        (json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip())
    }
    sets, notes = {}, {}
    for name in args.files:
        ans, note = load(name)
        missing = sorted(set(ids) - set(ans))
        if missing:
            print(f"{name} 缺题 {missing}", file=__import__("sys").stderr)
            return 1
        sets[name] = {rid: as_set(ans[rid]) for rid in ids}
        notes[name] = note

    core = [rid for rid in ids if items[rid]["type"] != "boundary"]
    answerable = [rid for rid in core if not items[rid]["must_refuse"]]

    pairs = {}
    for a, b in itertools.combinations(args.files, 2):
        strict = sum(sets[a][r] == sets[b][r] for r in core) / len(core)
        mean_j = sum(jaccard(sets[a][r], sets[b][r]) for r in core) / len(core)
        pairs[f"{a} × {b}"] = {
            "strict": round(strict, 4), "mean_jaccard": round(mean_j, 4),
        }

    per_item = []
    for rid in ids:
        votes = Counter(sets[n][rid] for n in args.files)
        top, cnt = votes.most_common(1)[0]
        per_item.append({
            "id": rid,
            "type": items[rid]["type"],
            "must_refuse": bool(items[rid]["must_refuse"]),
            "question": items[rid]["question"],
            "n_variants": len(votes),
            "unanimous": len(votes) == 1,
            "majority": cnt >= 2,
            "majority_count": cnt,
            "majority_picks": sorted(top),
            "counts": {n: len(sets[n][rid]) for n in args.files},
            "notes": {n: notes[n].get(rid, "") for n in args.files},
        })
    by_id = {x["id"]: x for x in per_item}

    def rate(flag, subset):
        return sum(by_id[r][flag] for r in subset) / len(subset)

    unanimous_core = rate("unanimous", core)
    majority_core = rate("majority", core)
    unanimous_all = rate("unanimous", ids)
    majority_all = rate("majority", ids)

    gold_sets = {rid: frozenset(gold[rid]["reference_atoms"]) for rid in answerable}
    vs_gold = {}
    for n in args.files:
        vs_gold[n] = round(
            sum(sets[n][r] == gold_sets[r] for r in answerable) / len(answerable), 4
        )
    maj_gold = round(
        sum(by_id[r]["majority"] and frozenset(by_id[r]["majority_picks"]) == gold_sets[r]
            for r in answerable) / len(answerable), 4
    )

    groups = defaultdict(list)
    for x in per_item:
        groups["拒答题" if x["must_refuse"] else "可答题"].append(x)
        groups[x["type"]].append(x)
    group_stats = {
        k: {"n": len(v),
            "unanimous": round(sum(x["unanimous"] for x in v) / len(v), 4),
            "majority": round(sum(x["majority"] for x in v) / len(v), 4)}
        for k, v in groups.items()
    }

    report = {
        "annotators": args.files,
        "n": len(ids), "judged_n": len(core),
        "pairs": pairs,
        "unanimous_all": round(unanimous_all, 4),
        "majority_all": round(majority_all, 4),
        "unanimous_excluding_boundary": round(unanimous_core, 4),
        "majority_excluding_boundary": round(majority_core, 4),
        "groups": group_stats,
        "vs_golden_set": {"per_annotator": vs_gold, "majority": maj_gold,
                          "answerable_n": len(answerable)},
        "items": per_item,
    }
    (BASE / "multi_agreement.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"参与标注员 {len(args.files)} 位：{', '.join(args.files)}")
    print("两两一致（排除 boundary 的判定口径）：")
    for k, v in pairs.items():
        print(f"  {k}: 完全一致 {v['strict']:.1%} | 平均 Jaccard {v['mean_jaccard']:.3f}")
    print(f"全体一致：总体 {unanimous_all:.1%} | 判定口径 {unanimous_core:.1%}")
    print(f"多数一致（≥2 人相同）：总体 {majority_all:.1%} | 判定口径 {majority_core:.1%}")
    print("分组（判定口径内）：")
    for k in ("可答题", "拒答题", "single_hop", "multi_hop", "instruction", "boundary", "refuse"):
        if k in group_stats:
            s = group_stats[k]
            print(f"  {k}: {s['n']} 题，全体一致 {s['unanimous']:.1%}，多数一致 {s['majority']:.1%}")
    print(f"与已标正本逐题相同（{len(answerable)} 道可答题）：")
    for n, v in vs_gold.items():
        print(f"  {n}: {v:.1%}")
    print(f"  多数意见 vs 正本: {maj_gold:.1%}")
    all_diff = [x["id"] for x in per_item if not x["majority"]]
    print(f"三人各不相同的题: {', '.join(all_diff) or '无'}")
    print(f"明细已写 {BASE / 'multi_agreement.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
