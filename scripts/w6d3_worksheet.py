#!/usr/bin/env python3
"""W6 D3 答题纸：build 生成人工标注工作面，apply 把人写下的答案回填 golden_set.jsonl。

脚本不生成、不猜测任何标准答案。它只做两件事：把冻结语料对应章节摘出来排好版，
以及把人在答题纸里写的内容搬回数据文件。任务卡 11/11 的卡点是「别用模型生成标准答案」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
CORPUS = EVALS / "corpus"
DATA = EVALS / "golden_set.jsonl"
SHEET = EVALS / "golden_set.worksheet.md"

F_CONFIRM = "题干确认"
F_ANSWER = "标准答案"
F_ATOMS = "判分要点"
F_BEHAVIOR = "期望行为"
F_CRITERIA = "拒答判据"

DOC_ORDER = [
    "evals/corpus/decisions.md",
    "evals/corpus/k8s-concepts.md",
    "evals/corpus/kubectl-cheatsheet.md",
    "evals/corpus/env-setup.md",
    "evals/corpus/docker-data-move-to-E.md",
    "evals/corpus/resource-ledger.md",
]
DOC_LABEL = {
    "evals/corpus/decisions.md": "决策日志 decisions.md",
    "evals/corpus/k8s-concepts.md": "K8s 概念 k8s-concepts.md",
    "evals/corpus/kubectl-cheatsheet.md": "kubectl 速查表",
    "evals/corpus/env-setup.md": "本机环境 env-setup.md",
    "evals/corpus/docker-data-move-to-E.md": "Docker 数据盘迁移",
    "evals/corpus/resource-ledger.md": "资源账本 resource-ledger.md",
}


def load_rows() -> list[dict]:
    return [
        json.loads(line)
        for line in DATA.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_corpus() -> dict[str, str]:
    return {
        f"evals/corpus/{p.name}": p.read_text(encoding="utf-8")
        for p in CORPUS.glob("*.md")
    }


def section_text(text: str, hint: str) -> str | None:
    """抽 hint 标题下、到下一个同级或更高级标题之前的正文（含代码块）。"""
    lines = text.splitlines()
    start, level = None, 2
    for i, line in enumerate(lines):
        if line.strip() == hint.strip():
            m = re.match(r"^(#{1,6})\s", line)
            level = len(m.group(1)) if m else 2
            start = i
            break
    if start is None:
        return None
    out: list[str] = []
    for line in lines[start + 1:]:
        m = re.match(r"^(#{1,6})\s", line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def fence_for(text: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`{3,}", text)), default=0)
    return "`" * max(4, longest + 1)


def question_block(row: dict) -> list[str]:
    rid = row["id"]
    lines = [
        f"#### {rid} · {row['type']}",
        "",
        f"题干（草案）：{row['question']}",
        "",
        f"- {F_CONFIRM}：",
    ]
    if row["must_refuse"]:
        lines += [
            f"- {F_BEHAVIOR}：",
            f"- {F_CRITERIA}：{row['refusal_criteria']}",
        ]
    else:
        lines += [
            f"- {F_ANSWER}：",
            f"- {F_ATOMS}：",
        ]
    lines.append("")
    return lines


def build(force: bool) -> int:
    if SHEET.exists() and not force:
        old = SHEET.read_text(encoding="utf-8")
        # 只认同一行冒号后面的内容；\s* 会跨行，把下一行的 "-" 误判成已填写
        if re.search(
            rf"^- (?:{F_CONFIRM}|{F_ANSWER}|{F_ATOMS}|{F_BEHAVIOR})：[ \t]*\S", old, re.M
        ):
            print("答题纸里已有填写内容，拒绝覆盖；确要重建加 --force（会丢已填答案）",
                  file=sys.stderr)
            return 1

    rows = load_rows()
    corpus = load_corpus()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    refuses: list[dict] = []
    for row in rows:
        if row["must_refuse"]:
            refuses.append(row)
        else:
            groups[(row["source_doc"], row["source_hint"])].append(row)

    missing: list[str] = []
    by_doc: dict[str, list[tuple[str, list[dict]]]] = defaultdict(list)
    for (doc, hint), items in groups.items():
        by_doc[doc].append((hint, items))
    stray_docs = sorted(set(by_doc) - set(DOC_ORDER))
    if stray_docs:
        print(f"⚠️ 有题目指向未列入分组顺序的语料：{stray_docs}", file=sys.stderr)

    out: list[str] = [
        "# W6 D3 答题纸 · 50 题人工标注",
        "",
        "> [!important] 怎么填",
        "> 这份纸只用来**搬运人写的答案**，AI 不代写标准答案（任务卡 11/11 卡点原话）。",
        "> 每题三行，写在冒号后面、同一行；「题干确认」没改就填 `OK`，改了就写新的真实问法。",
        "> 可答题的「标准答案」从上方原文块里**逐字复制**，别自己概括；「判分要点」拆 2–5 条、用 `；` 分隔，每条都要能在原文找到。",
        "> 拒答题没有原文，填「期望行为」（例如说明文档未覆盖并拒答），判据已给草案、可改。",
        "> 填完跑：`./.venv-eval/Scripts/python.exe scripts/w6d3_worksheet.py --apply --by <你的代号>`，先加 `--dry-run` 预览。",
        "",
        "## 进度",
        "",
        "| 组 | 题数 | 备注 |",
        "| --- | --- | --- |",
    ]
    for doc in DOC_ORDER:
        n = sum(1 for r in rows if r["source_doc"] == doc)
        out.append(f"| {DOC_LABEL[doc]} | {n} | 原文按章节列在下面 |")
    out.append(f"| 应该拒答（语料外） | {len(refuses)} | 无原文，只填期望行为 |")
    out += ["", "---", ""]

    for idx, doc in enumerate(DOC_ORDER, 1):
        sections = by_doc.get(doc, [])
        total = sum(len(items) for _, items in sections)
        out += [f"## {'一二三四五六'[idx - 1]}、{DOC_LABEL[doc]}（{total} 题）", ""]
        for hint, items in sections:
            text = section_text(corpus.get(doc, ""), hint)
            title = re.sub(r"^#{1,6}\s*", "", hint).strip()
            out += [f"### 📄 原文：{title}", ""]
            if text is None:
                missing.append(f"{doc} :: {hint}")
                out += ["> ⚠️ 未能在冻结语料里定位这个章节，请手工翻原文。", ""]
            else:
                fence = fence_for(text)
                out += [f"{fence}text", text, fence, ""]
            out.append(f"本节 {len(items)} 题，答案从上方原文逐字摘：")
            out.append("")
            for row in items:
                out += question_block(row)
        out += ["---", ""]

    out += [
        f"## 七、应该拒答（{len(refuses)} 题，语料外）",
        "",
        "> 这些题没有原文块。正确行为是说明文档未覆盖并拒答，不给具体数值、步骤或 API（应用程序接口）。判据草案已写在每题里，可改。",
        "",
    ]
    for row in refuses:
        out += question_block(row)

    out += [
        "---",
        "",
        "## 回填",
        "",
        "```bash",
        "cd /e/Projects/LLMOps-Engineer",
        "./.venv-eval/Scripts/python.exe scripts/w6d3_worksheet.py --apply --by LEGO --dry-run",
        "./.venv-eval/Scripts/python.exe scripts/w6d3_worksheet.py --apply --by LEGO",
        "./.venv-eval/Scripts/python.exe scripts/w6d3_validate_golden.py",
        "```",
        "",
        "回填只写人在纸上填过的字段，不会覆盖已有答案；`--strict` 退出码变 0 才算 50 题标完。",
        "",
    ]
    SHEET.write_text("\n".join(out), encoding="utf-8")

    if missing:
        print(f"⚠️ {len(missing)} 个章节未能定位：", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
    print(f"已生成 {SHEET}（{len(rows)} 题 / {len(groups)} 个原文块 / "
          f"{len(refuses)} 道拒答题），未写任何标准答案。")
    return 0


def read_field(block: str, name: str) -> str:
    m = re.search(rf"^- {re.escape(name)}：(.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def apply(by: str, dry_run: bool, force: bool, when: str) -> int:
    if not SHEET.exists():
        print("答题纸不存在，先跑 --build", file=sys.stderr)
        return 1
    content = SHEET.read_text(encoding="utf-8")
    blocks = re.split(r"(?=^#### g\d{3} )", content, flags=re.M)
    updates: dict[str, dict[str, str]] = {}
    for block in blocks:
        m = re.match(r"^#### (g\d{3})\s", block)
        if not m:
            continue
        confirm = read_field(block, F_CONFIRM)
        answer = read_field(block, F_ANSWER) or read_field(block, F_BEHAVIOR)
        atoms = read_field(block, F_ATOMS)
        criteria = read_field(block, F_CRITERIA)
        if not any((confirm, answer, atoms)):
            continue
        updates[m.group(1)] = {
            "confirm": confirm,
            "answer": answer,
            "atoms": atoms,
            "criteria": criteria,
        }

    if not updates:
        print("答题纸里没有可回填的填写内容（三行都空着）。")
        return 0

    rows = load_rows()
    known = {r["id"] for r in rows}
    unknown = sorted(set(updates) - known)
    if unknown:
        print(f"答题纸里有数据文件中不存在的题号：{unknown}", file=sys.stderr)
        return 1

    errors: list[str] = []
    changed: list[str] = []
    for row in rows:
        u = updates.get(row["id"])
        if not u:
            continue
        if u["confirm"]:
            value = u["confirm"]
            if value.upper() not in ("OK", "OK.", "Y", "YES", "确认", "已核", "已确认", "照原", "原样", "✔", "✓"):
                if row["question"] != value:
                    # 草案题干待人改成真实问法，第一次确认不需要 force；
                    # 已经有人确认过再改，才要求显式 --force。
                    if row["question_confirmed_by"] and not force:
                        errors.append(
                            f"{row['id']} 题干已被 {row['question_confirmed_by']} 确认过，需 --force 才改"
                        )
                    else:
                        row["question"] = value
                        row["question_origin"] = "human_real_query"
            row["question_confirmed_by"] = by
        if u["answer"]:
            if row["reference"] and row["reference"] != u["answer"] and not force:
                errors.append(f"{row['id']} 已有标准答案，需 --force 才覆盖")
            else:
                row["reference"] = u["answer"]
                row["labeled_by"] = by
                row["labeled_at"] = when
        if u["atoms"]:
            parts = [p.strip() for p in re.split(r"[；;]", u["atoms"]) if p.strip()]
            row["reference_atoms"] = parts
        if u["criteria"] and row["must_refuse"]:
            row["refusal_criteria"] = u["criteria"]
        changed.append(row["id"])

    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"将回填 {len(changed)} 题：{', '.join(changed)}")
    print(f"标注人 {by} / 日期 {when} / dry_run={dry_run}")
    if dry_run:
        print("--dry-run：未写文件。")
        return 0
    DATA.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"已写回 {DATA}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="生成答题纸")
    group.add_argument("--apply", action="store_true", help="把人填的答案回填数据文件")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有内容（谨慎）")
    parser.add_argument("--by", default="", help="标注人代号，写进 labeled_by / question_confirmed_by")
    parser.add_argument("--date", default="", help="标注日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="只预览回填，不写文件")
    args = parser.parse_args()

    if args.build:
        return build(args.force)
    if not args.by:
        print("--apply 需要 --by <标注人代号>", file=sys.stderr)
        return 1
    return apply(args.by, args.dry_run, args.force, args.date or date.today().isoformat())


if __name__ == "__main__":
    raise SystemExit(main())
