#!/usr/bin/env python3
"""W6 D3 双人复标一致性：生成复标模板、算逐题一致率与 Cohen's kappa、列分歧题。

复标文件是本机产物，落在 evals/out/w6d3/（已被 .gitignore 忽略）。
退出码：0 = 通过或仅自测；1 = 数据有问题；2 = kappa 未达 0.8 验收线。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
OUT = EVALS / "out" / "w6d3"

LABELS = ("correct", "partial", "incorrect", "refused_ok", "refused_fail")
REFUSE_LABELS = {"refused_ok", "refused_fail"}
KAPPA_GATE = 0.8
WHO_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def load_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    if not path.exists():
        return rows, [f"文件不存在: {path}"]
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{lineno} JSON 解析失败: {exc}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{path.name}:{lineno} 每行必须是 JSON 对象")
            continue
        rows.append(obj)
    return rows, errors


def cohen_kappa(a: list[str], b: list[str]) -> tuple[float, float, float]:
    """返回 (观察一致率 po, 期望一致率 pe, kappa)。pe=1 时 kappa 无定义，记 0。"""
    n = len(a)
    if n == 0:
        return 0.0, 0.0, 0.0
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    labels = set(ca) | set(cb)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in labels)
    kappa = (po - pe) / (1 - pe) if pe != 1 else 0.0
    return po, pe, kappa


def init_template(who: str, force: bool) -> int:
    if not WHO_PATTERN.match(who):
        print(f"标注者代号只能含字母数字下划线连字符: {who!r}", file=sys.stderr)
        return 1
    rows, errors = load_jsonl(EVALS / "golden_set.jsonl")
    if errors:
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"relabel_{who}.jsonl"
    if target.exists() and not force:
        print(f"已存在 {target}，拒绝覆盖（人工标注可能已填）。确要重建加 --force", file=sys.stderr)
        return 1
    lines = [
        json.dumps(
            {
                "id": row["id"],
                "question": row["question"],
                "must_refuse": row["must_refuse"],
                "label": "",
                "note": "",
            },
            ensure_ascii=False,
        )
        for row in rows
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {target}（{len(lines)} 题，label 全空）。两人各自独立填写，不要商量。")
    return 0


def check_labels(rows: list[dict], who: str) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    for idx, row in enumerate(rows, 1):
        rid = row.get("id", f"<第{idx}行>")
        if rid in ids:
            errors.append(f"{who}:{idx} id 重复: {rid}")
        ids.add(rid)
        label = row.get("label", "")
        if not label:
            errors.append(f"{who}:{idx} ({rid}) label 为空")
            continue
        if label not in LABELS:
            errors.append(f"{who}:{idx} ({rid}) label 非法: {label}")
            continue
        is_refuse = row.get("must_refuse") is True
        if is_refuse and label not in REFUSE_LABELS:
            errors.append(f"{who}:{idx} ({rid}) 拒答题应标 refused_ok / refused_fail")
        if not is_refuse and label in REFUSE_LABELS:
            errors.append(f"{who}:{idx} ({rid}) 可答题不能用 refused_* 标签")
    return errors


def compare(a_name: str, b_name: str) -> int:
    a_rows, a_errors = load_jsonl(OUT / f"relabel_{a_name}.jsonl")
    b_rows, b_errors = load_jsonl(OUT / f"relabel_{b_name}.jsonl")
    errors = a_errors + b_errors
    errors.extend(check_labels(a_rows, a_name))
    errors.extend(check_labels(b_rows, b_name))
    if errors:
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    a_map = {row["id"]: row for row in a_rows}
    b_map = {row["id"]: row for row in b_rows}
    only_a = sorted(set(a_map) - set(b_map))
    only_b = sorted(set(b_map) - set(a_map))
    if only_a or only_b:
        print(f"两份复标题号不一致：只在 {a_name} 有 {only_a}，只在 {b_name} 有 {only_b}",
              file=sys.stderr)
        return 1

    ids = sorted(a_map)
    a = [a_map[i]["label"] for i in ids]
    b = [b_map[i]["label"] for i in ids]
    po, pe, kappa = cohen_kappa(a, b)
    disagreements = [
        {
            "id": rid,
            "question": a_map[rid].get("question", ""),
            "must_refuse": a_map[rid].get("must_refuse", False),
            a_name: a_map[rid]["label"],
            b_name: b_map[rid]["label"],
            "note_a": a_map[rid].get("note", ""),
            "note_b": b_map[rid].get("note", ""),
        }
        for rid in ids
        if a_map[rid]["label"] != b_map[rid]["label"]
    ]
    groups = {}
    for flag, name in ((True, "refuse"), (False, "answerable")):
        subset = [i for i in ids if bool(a_map[i].get("must_refuse")) is flag]
        if subset:
            sub_po, _, sub_kappa = cohen_kappa(
                [a_map[i]["label"] for i in subset],
                [b_map[i]["label"] for i in subset],
            )
            groups[name] = {"n": len(subset), "agreement": round(sub_po, 4),
                            "kappa": round(sub_kappa, 4)}

    report = {
        "annotators": [a_name, b_name],
        "n": len(ids),
        "observed_agreement": round(po, 4),
        "expected_agreement": round(pe, 4),
        "cohen_kappa": round(kappa, 4),
        "gate": KAPPA_GATE,
        "groups": groups,
        "label_distribution": {a_name: dict(Counter(a)), b_name: dict(Counter(b))},
        "disagreements": disagreements,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "agreement.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"两人复标 {len(ids)} 题 | 逐题一致率 {po:.3f} | 期望一致率 {pe:.3f} "
          f"| Cohen's kappa {kappa:.3f}（验收线 {KAPPA_GATE}）")
    for name, group in groups.items():
        print(f"  {name}: {group['n']} 题，一致率 {group['agreement']}，kappa {group['kappa']}")
    print(f"分歧 {len(disagreements)} 题：{', '.join(d['id'] for d in disagreements) or '无'}")
    print(f"明细已写 {OUT / 'agreement.json'}")
    if kappa < KAPPA_GATE:
        print("kappa 未达验收线：先改 ANNOTATION.md 的判据措辞，两人重新独立标，别协商改标。",
              file=sys.stderr)
        return 2
    return 0


def self_test() -> int:
    """不依赖真人复标，用三组已知答案验算法，证明脚本本身没写错。"""
    a = ["correct"] * 5 + ["partial"] * 3 + ["incorrect"] * 2
    b = ["correct"] * 5 + ["partial"] * 2 + ["incorrect"] * 2 + ["partial"]
    po, pe, kappa = cohen_kappa(a, b)
    assert abs(po - 0.8) < 1e-9, po
    assert abs(pe - 0.38) < 1e-9, pe
    assert abs(kappa - 0.42 / 0.62) < 1e-9, kappa
    assert cohen_kappa(a, a)[2] == 1.0
    c = ["correct"] * 5 + ["partial"] * 5
    d = ["partial"] * 5 + ["correct"] * 5
    assert abs(cohen_kappa(c, d)[2] + 1.0) < 1e-9
    assert cohen_kappa(["correct"] * 3, ["correct"] * 3)[1] == 1.0
    print("self-test 通过：8/10 一致 → po=0.800 / pe=0.380 / kappa=0.677；"
          "完全一致 kappa=1.000；完全错开 kappa=-1.000；单类标注 pe=1 不崩。")
    print("⚠️ 这只是算法自测，真人的两人独立复标尚未进行，验收不算通过。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", metavar="WHO", help="生成标注者 WHO 的空复标模板")
    group.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比较两份复标结果")
    group.add_argument("--self-test", action="store_true", help="用内置样例验算法")
    parser.add_argument("--force", action="store_true", help="--init 时允许覆盖已有模板")
    args = parser.parse_args()

    if args.init:
        return init_template(args.init, args.force)
    if args.compare:
        return compare(*args.compare)
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
