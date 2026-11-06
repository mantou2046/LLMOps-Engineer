#!/usr/bin/env python3
"""把网页版答题页导出的 w6d3_quiz_answers.json 回填到 golden_set.jsonl。

只搬运人在网页上点选的原文句子，不生成、不改写任何答案文本。
用法：./.venv-eval/Scripts/python.exe scripts/w6d3_apply_quiz.py --file <导出文件> --by <代号>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals" / "golden_set.jsonl"
QUIZ_HTML = ROOT / "evals" / "golden_set.quiz.html"


def pretty(s: str) -> str:
    """与 w6d3_web_quiz.py 的显示规则一致。"""
    return re.sub(r"[*`]", "", re.sub(r"^(?:>\s*|#\s*)", "", s)).strip()


def display_maps() -> dict:
    """读网页内嵌数据，建「显示文本 -> 原文逐字值」映射。"""
    if not QUIZ_HTML.exists():
        return {}
    m = re.search(r"const DATA = (\{.*?\});\n", QUIZ_HTML.read_text(encoding="utf-8"), re.S)
    if not m:
        return {}
    data = json.loads(m.group(1))
    out = {}
    for it in data["items"]:
        pmap = {}
        for v in it["values"]:
            pmap.setdefault(pretty(v), v)
        out[it["id"]] = (set(it["values"]), pmap)
    return out


def normalize_answers(answers: dict) -> int:
    """导出文件若存的是网页显示文本，还原成原文逐字值；不改选择，只改文本形态。

    有些导出是按 options（显示用、已去 Markdown 标记）生成的，直接回填会核不回语料。
    """
    maps = display_maps()
    fixed = 0
    for rid, a in answers.items():
        if a.get("must_refuse"):
            continue
        atoms = [str(x) for x in a.get("atoms", [])]
        if not atoms:
            continue
        rawset, pmap = maps.get(rid, (set(), {}))
        restored = []
        for x in atoms:
            if x in rawset or x not in pmap:
                restored.append(x)
            else:
                restored.append(pmap[x])
                fixed += 1
        if a.get("reference") == "\n".join(atoms):
            a["reference"] = "\n".join(restored)
        a["atoms"] = restored
    return fixed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="网页导出的答案 JSON 路径")
    parser.add_argument("--by", required=True, help="标注人代号")
    parser.add_argument("--date", default="", help="标注日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有答案/已确认题干")
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"找不到导出文件：{src}", file=sys.stderr)
        return 1
    try:
        obj = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"答案 JSON 解析失败：{exc}", file=sys.stderr)
        return 1
    answers = obj.get("answers")
    if not isinstance(answers, dict):
        print("答案 JSON 缺少 answers 对象", file=sys.stderr)
        return 1
    fixed = normalize_answers(answers)
    if fixed:
        print(f"已把 {fixed} 条显示文本还原成语料原文（选择不变）")

    rows = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
    known = {r["id"] for r in rows}
    unknown = sorted(set(answers) - known)
    if unknown:
        print(f"导出文件里有数据集中不存在的题号：{unknown}", file=sys.stderr)
        return 1

    when = args.date or date.today().isoformat()
    errors: list[str] = []
    changed: list[str] = []
    for row in rows:
        a = answers.get(row["id"])
        if not a:
            continue
        new_ref = str(a.get("reference", "")).strip()
        if not new_ref:
            errors.append(f"{row['id']} 没有标准答案，跳过")
            continue
        atoms = [str(x).strip() for x in a.get("atoms", []) if str(x).strip()]

        if a.get("question_changed"):
            new_q = str(a.get("question", "")).strip()
            if new_q and new_q != row["question"]:
                if row["question_confirmed_by"] and not args.force:
                    errors.append(
                        f"{row['id']} 题干已被 {row['question_confirmed_by']} 确认过，需 --force 才改"
                    )
                else:
                    row["question"] = new_q
                    row["question_origin"] = "human_real_query"
        row["question_confirmed_by"] = args.by

        if row["reference"] and row["reference"] != new_ref and not args.force:
            errors.append(f"{row['id']} 已有标准答案，需 --force 才覆盖")
            continue
        row["reference"] = new_ref
        row["reference_atoms"] = atoms
        if row["must_refuse"] and a.get("criteria"):
            row["refusal_criteria"] = str(a["criteria"])
        row["labeled_by"] = args.by
        row["labeled_at"] = when
        changed.append(row["id"])

    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"将回填 {len(changed)} 题：{', '.join(changed) or '无'}")
    print(f"标注人 {args.by} / 日期 {when} / dry_run={args.dry_run}")
    if args.dry_run:
        print("--dry-run：未写文件。")
        return 0
    DATA.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"已写回 {DATA}")
    print("下一步：./.venv-eval/Scripts/python.exe scripts/w6d3_validate_golden.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
