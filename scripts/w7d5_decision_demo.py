"""W7 D5 · 决策对照表验证：固定场景集 + 证据指针体检。

任务卡 11/20 要求「每个结论都由 11/19 实测或 W6 数字撑着」。
本脚本把这句话做成可跑的检查：
  · 12 条场景逐条过策略选择器，与人工标注比对一致率；
  · 每条推荐都必须带**非空且存在于证据索引**的编号；
  · 凡推荐「微调」，必须带「业务效果未量」声明（D4 云端已跑通真训练，但只有小样本格式证据）。

用法（仓库根目录）：
  D:\\python314\\python.exe scripts\\w7d5_decision_demo.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import decision  # noqa: E402

SCENARIOS = ROOT / "evals" / "decision" / "scenarios.jsonl"
OUT = ROOT / "evals" / "out" / "w7d5" / "decision_bench.json"


def load_scenarios() -> list[dict]:
    return [
        json.loads(line)
        for line in SCENARIOS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    scenarios = load_scenarios()
    rows: list[dict] = []
    problems: list[str] = []
    for s in scenarios:
        f = decision.TaskFeatures(name=s["name"], **s["features"])
        rec = decision.decide(f)
        issues = decision.validate(rec)
        expected = s["expected_primary"]
        if rec.primary != expected:
            issues.append(f"与标注不符：期望 {expected}、实际 {rec.primary}")
        problems.extend(f"{s['id']}: {x}" for x in issues)
        rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                "expected": expected,
                "actual": rec.primary,
                "also": list(rec.also),
                "evidence": list(rec.evidence),
                "caveats": list(rec.caveats),
                "note": s["note"],
                "ok": not issues,
            }
        )

    matched = sum(1 for r in rows if r["actual"] == r["expected"])
    with_evidence = sum(1 for r in rows if r["evidence"])
    ft_rows = [r for r in rows if r["actual"] == "微调"]
    ft_with_caveat = sum(
        1 for r in ft_rows if any("效果未量" in c for c in r["caveats"])
    )
    distribution = dict(Counter(r["actual"] for r in rows))

    result = {
        "scenarios": len(rows),
        "agreement": round(matched / len(rows), 4),
        "evidence_coverage": round(with_evidence / len(rows), 4),
        "finetune_recommendations": len(ft_rows),
        "finetune_with_caveat": ft_with_caveat,
        "distribution": distribution,
        "evidence_index_size": len(decision.EVIDENCE_INDEX),
        "problems": problems,
        "rows": rows,
        "caveats": [
            "证据全部来自 W6/W7 已落库报告，不含本次新编的数字",
            "真微调业务效果未量（D4 云端只有小样本格式证据），微调结论都带此声明",
            "12 条场景是演示规模，不等于真实业务分布",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ID   场景                                       期望    实际    证据数  通过")
    for r in rows:
        name = r["name"][:24]
        print(
            f"{r['id']:<5} {name:<26} {r['expected']:<7} {r['actual']:<7} "
            f"{len(r['evidence']):>5}   {'OK' if r['ok'] else 'FAIL'}"
        )
    print(f"\n一致率 {result['agreement']:.1%}（{matched}/{len(rows)}）｜"
          f"证据覆盖 {result['evidence_coverage']:.1%}｜"
          f"微调推荐 {len(ft_rows)} 条，带未量声明 {ft_with_caveat} 条")
    print("结论分布：" + "、".join(f"{k} {v}" for k, v in sorted(distribution.items())))
    print(f"证据索引 {len(decision.EVIDENCE_INDEX)} 条，明细：{OUT.relative_to(ROOT)}")
    if problems:
        print("问题：" + "；".join(problems))

    assert not problems, "场景集必须零问题"
    assert matched == len(rows), "与人工标注必须全一致"
    assert with_evidence == len(rows), "每条结论都必须带证据指针"
    assert ft_with_caveat == len(ft_rows), "每条微调推荐都必须声明效果未量"
    assert distribution.get("微调", 0) <= 2, "微调不该是默认答案"
    print("\n断言 5/5 通过：全一致、全覆盖、微调全声明、微调不是默认解。")


if __name__ == "__main__":
    main()
