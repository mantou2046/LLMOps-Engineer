#!/usr/bin/env python3
"""比对两位独立标注员的盲标结果，出逐题一致率与分歧清单。

比的是「各自从原文候选里选出的句子集合」，不是五档 label 的 kappa。
用法：./.venv-eval/Scripts/python.exe scripts/w6d3_blind_agreement.py --a blind_ds.json --b blind_codex.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "evals" / "out" / "w6d3"
ITEMS = BASE / "blind_items.json"


def load(name: str) -> tuple[dict, dict]:
    obj = json.loads((BASE / name).read_text(encoding="utf-8"))
    return obj.get("answers", obj), obj.get("notes", {})


def jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def as_set(v) -> set:
    if isinstance(v, list):
        return set(v)
    return {str(v)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", default="blind_ds.json")
    p.add_argument("--b", default="blind_codex.json")
    p.add_argument("--gate", type=float, default=0.8, help="严格一致率验收线")
    args = p.parse_args()

    A, notes_a = load(args.a)
    B, notes_b = load(args.b)
    items = {x["id"]: x for x in json.loads(ITEMS.read_text(encoding="utf-8"))}
    ids = sorted(items)
    missing_a, missing_b = sorted(set(ids) - set(A)), sorted(set(ids) - set(B))
    if missing_a or missing_b:
        print(f"缺题 A={missing_a} B={missing_b}", file=__import__("sys").stderr)
        return 1

    rows = []
    for rid in ids:
        it = items[rid]
        sa, sb = as_set(A[rid]), as_set(B[rid])
        identical = sa == sb
        j = jaccard(list(sa), list(sb))
        rows.append({
            "id": rid,
            "type": it["type"],
            "must_refuse": bool(it["must_refuse"]),
            "question": it["question"],
            "identical": identical,
            "jaccard": round(j, 4),
            "n_a": len(sa) if not it["must_refuse"] else 1,
            "n_b": len(sb) if not it["must_refuse"] else 1,
            "only_a": sorted(sa - sb),
            "only_b": sorted(sb - sa),
            "note_a": notes_a.get(rid, ""),
            "note_b": notes_b.get(rid, ""),
        })

    n = len(rows)
    # boundary 题文档本来就只覆盖一部分，按 ANNOTATION.md 的口径单独成组报分，
    # 不进总体判定；总体数字仍然双报，避免看起来像挑数据。
    core = [r for r in rows if r["type"] != "boundary"]
    boundary = [r for r in rows if r["type"] == "boundary"]
    strict = sum(r["identical"] for r in rows) / n
    mean_j = sum(r["jaccard"] for r in rows) / n
    strict_core = sum(r["identical"] for r in core) / len(core)
    mean_j_core = sum(r["jaccard"] for r in core) / len(core)
    groups = defaultdict(list)
    for r in rows:
        groups["拒答题" if r["must_refuse"] else "可答题"].append(r)
        groups[r["type"]].append(r)
    group_stats = {
        k: {"n": len(v), "strict": round(sum(x["identical"] for x in v) / len(v), 4),
            "mean_jaccard": round(sum(x["jaccard"] for x in v) / len(v), 4)}
        for k, v in groups.items()
    }
    diffs = [r for r in rows if not r["identical"]]

    report = {
        "a": args.a, "b": args.b, "n": n,
        "strict_agreement": round(strict, 4),
        "mean_jaccard": round(mean_j, 4),
        "strict_agreement_excluding_boundary": round(strict_core, 4),
        "mean_jaccard_excluding_boundary": round(mean_j_core, 4),
        "boundary_n": len(boundary),
        "judged_on": "excluding_boundary",
        "gate": args.gate,
        "passed": strict_core >= args.gate,
        "groups": group_stats,
        "disagreements": diffs,
    }
    (BASE / "blind_agreement.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"两位独立标注员：{args.a} vs {args.b}（{n} 题）")
    print(f"总体（含 {len(boundary)} 道 boundary，仅参考）：完全一致 {strict:.1%} | 平均 Jaccard {mean_j:.3f}")
    print(f"判定口径（排除 boundary，{len(core)} 题）：完全一致 {strict_core:.1%} | "
          f"平均 Jaccard {mean_j_core:.3f} | 验收线 {args.gate:.0%} → "
          f"{'通过' if report['passed'] else '未通过'}")
    for k in ("可答题", "拒答题"):
        s = group_stats[k]
        print(f"  {k}: {s['n']} 题，完全一致 {s['strict']:.1%}，平均 Jaccard {s['mean_jaccard']:.3f}")
    print("按题型：")
    for k in ("single_hop", "multi_hop", "instruction", "boundary", "refuse"):
        if k in group_stats:
            s = group_stats[k]
            print(f"  {k}: {s['n']} 题，完全一致 {s['strict']:.1%}")
    print(f"分歧 {len(diffs)} 题：{', '.join(r['id'] for r in diffs) or '无'}")
    print(f"明细已写 {BASE / 'blind_agreement.json'}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
