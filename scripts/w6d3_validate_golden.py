#!/usr/bin/env python3
"""W6 D3 golden set 校验：格式、出处可核性、答案可核性、分布与真实标注进度。

规则正本是 evals/golden_set.schema.json，本脚本只解释它，不另写一套字段表。
退出码：0 = 校验通过（进度未完成不算错）；1 = 结构或可核性有误；2 = --strict 下进度未达标。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
CORPUS = EVALS / "corpus"
OUT = EVALS / "out" / "w6d3"

EXAMPLE_MARK = "EXAMPLE_ONLY"
CHUNK_TOTAL = 330  # W6 D1 递归 B（400/50）切冻结语料得到的块数
MIN_REFUSE = 10


def norm(text: str) -> str:
    """把连续空白压成单空格，让跨行的原文摘引也能逐字核回去。"""
    return re.sub(r"\s+", " ", text or "").strip()


def load_jsonl(path: Path) -> tuple[list[tuple[int, dict]], list[str]]:
    rows: list[tuple[int, dict]] = []
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
        rows.append((lineno, obj))
    return rows, errors


def type_matches(value: object, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    if name in ("integer", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def check_value(value: object, rule: dict, where: str, errors: list[str]) -> None:
    expected = rule.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(type_matches(value, name) for name in names):
            errors.append(
                f"{where} 类型应为 {'/'.join(names)}，实际 {type(value).__name__}"
            )
    if "enum" in rule and value not in rule["enum"]:
        errors.append(f"{where} 取值 {value!r} 不在枚举 {rule['enum']}")
    if "const" in rule and value != rule["const"]:
        errors.append(f"{where} 应为 {rule['const']!r}，实际 {value!r}")
    if "minLength" in rule and isinstance(value, str) and len(value) < rule["minLength"]:
        errors.append(f"{where} 长度 {len(value)} 小于 {rule['minLength']}")
    if "pattern" in rule and isinstance(value, str) and value != "":
        if not re.search(rule["pattern"], value):
            errors.append(f"{where} 不匹配 {rule['pattern']}")
    if "items" in rule and isinstance(value, list):
        for idx, item in enumerate(value):
            check_value(item, rule["items"], f"{where}[{idx}]", errors)


def check_row(row: dict, schema: dict, where: str, errors: list[str]) -> None:
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in row:
            errors.append(f"{where} 缺字段 {key}")
    if schema.get("additionalProperties") is False:
        for key in row:
            if key not in props:
                errors.append(f"{where} 多了未定义字段 {key}")
    for key, rule in props.items():
        if key in row:
            check_value(row[key], rule, f"{where}.{key}", errors)
    for clause in schema.get("allOf", []):
        cond = clause.get("if", {}).get("properties", {})
        matched = all(
            row.get(key) == rule["const"]
            for key, rule in cond.items()
            if "const" in rule
        )
        if matched:
            for key, rule in clause.get("then", {}).get("properties", {}).items():
                if key in row:
                    check_value(row[key], rule, f"{where}.{key}(条件)", errors)


def read_corpus() -> tuple[dict[str, str], list[str]]:
    """读冻结六篇，并核对 manifest 里的 sha256，防止语料被悄悄改长。"""
    errors: list[str] = []
    manifest_path = CORPUS / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    texts: dict[str, str] = {}
    for entry in manifest["files"]:
        rel = f"evals/corpus/{entry['file']}"
        path = CORPUS / entry["file"]
        if not path.exists():
            errors.append(f"冻结语料缺文件: {rel}")
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry["sha256"]:
            errors.append(f"冻结语料被改动: {rel}（sha256 不符 manifest）")
        if len(raw) != entry["bytes"]:
            errors.append(f"冻结语料体积变化: {rel}")
        texts[rel] = raw.decode("utf-8")
    return texts, errors


def is_real(value: str) -> bool:
    return bool(value) and value != EXAMPLE_MARK


def validate(rows: list[tuple[int, dict]], schema: dict, corpus: dict[str, str],
             label: str) -> tuple[list[str], dict]:
    errors: list[str] = []
    seen: set[str] = set()
    types: Counter[str] = Counter()
    docs: Counter[str] = Counter()
    refuse = 0
    confirmed = 0
    labeled = 0
    verbatim = 0
    atom_based = 0
    chunks = 0

    for lineno, row in rows:
        where = f"{label}:{lineno}"
        check_row(row, schema, where, errors)
        rid = row.get("id", f"<第{lineno}行>")
        if rid in seen:
            errors.append(f"{where} id 重复: {rid}")
        seen.add(rid)

        must_refuse = row.get("must_refuse") is True
        doc = row.get("source_doc")
        hint = row.get("source_hint", "")
        criteria = row.get("refusal_criteria", "")
        rtype = row.get("type", "")
        reference = row.get("reference", "")
        atoms = row.get("reference_atoms", [])

        types[rtype] += 1
        if must_refuse:
            refuse += 1
            if rtype != "refuse":
                errors.append(f"{where} must_refuse=true 时 type 必须为 refuse")
            if doc is not None:
                errors.append(f"{where} 拒答题的 source_doc 应为 null")
            if not criteria:
                errors.append(f"{where} 拒答题缺 refusal_criteria")
        else:
            if criteria:
                errors.append(f"{where} 可答题不该填 refusal_criteria")
            if rtype == "refuse":
                errors.append(f"{where} 可答题的 type 不能是 refuse")
            docs[doc or "<空>"] += 1
            if doc not in corpus:
                if doc is not None:
                    errors.append(f"{where} source_doc 不在冻结语料清单里: {doc}")
            elif hint and norm(hint) not in norm(corpus[doc]):
                errors.append(f"{where} source_hint 在原文中找不到: {hint}")

        if is_real(row.get("question_confirmed_by", "")):
            confirmed += 1
        if is_real(row.get("labeled_by", "")):
            labeled += 1
            if not reference:
                errors.append(f"{where} 标了 labeled_by 却没有 reference")

        # 答案可核性：可答题的 reference 必须核回冻结语料原文
        if reference and not must_refuse and doc in corpus:
            text = norm(corpus[doc])
            if norm(reference) in text:
                verbatim += 1
            elif atoms and all(norm(a) in text for a in atoms):
                atom_based += 1
            else:
                errors.append(
                    f"{where} reference 既不是原文逐字摘引，atoms 也未能全部核回原文"
                )
            for idx, atom in enumerate(atoms):
                if norm(atom) not in text:
                    errors.append(f"{where} reference_atoms[{idx}] 在原文中找不到: {atom}")

        chunk = row.get("source_chunk")
        if chunk:
            base, _, idx = chunk.partition("#")
            if base not in {Path(p).name for p in corpus}:
                errors.append(f"{where} source_chunk 的文件名不在语料里: {chunk}")
            elif not idx.isdigit() or not 0 <= int(idx) < CHUNK_TOTAL:
                errors.append(f"{where} source_chunk 块号超出 0..{CHUNK_TOTAL - 1}: {chunk}")
            else:
                chunks += 1

    stats = {
        "total": len(rows),
        "type_distribution": dict(types),
        "source_doc_distribution": dict(docs),
        "must_refuse": refuse,
        "questions_confirmed": confirmed,
        "answers_labeled": labeled,
        "answers_verbatim": verbatim,
        "answers_atom_based": atom_based,
        "source_chunk_filled": chunks,
    }
    return errors, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="evals/golden_set.jsonl", help="题库 JSONL 相对仓库根的路径")
    parser.add_argument("--sample", action="store_true", help="额外校验示范文件")
    parser.add_argument("--strict", action="store_true", help="把「答案未标完」也当作失败（未来 CI 用）")
    args = parser.parse_args()

    schema = json.loads((EVALS / "golden_set.schema.json").read_text(encoding="utf-8"))
    corpus, corpus_errors = read_corpus()
    errors = list(corpus_errors)

    targets = [(args.data, "golden_set")]
    if args.sample:
        targets.append(("evals/golden_set.sample.jsonl", "sample"))
    all_stats = {}
    for rel, label in targets:
        rows, load_errors = load_jsonl(ROOT / rel)
        errors.extend(load_errors)
        row_errors, stats = validate(rows, schema, corpus, label)
        errors.extend(row_errors)
        all_stats[label] = stats

    # 题量与拒答题占比是整套 50 题的设计约束，只约束主数据集；
    # --sample 只是 4 条格式示范，不该被这两条下限卡住。
    main_stats = all_stats["golden_set"]
    if main_stats["must_refuse"] < MIN_REFUSE:
        errors.append(f"应拒答题只有 {main_stats['must_refuse']} 道，少于下限 {MIN_REFUSE}")
    if main_stats["total"] < 50:
        errors.append(f"题目只有 {main_stats['total']} 道，少于 50")

    OUT.mkdir(parents=True, exist_ok=True)
    report = {"errors": errors, "stats": all_stats}
    (OUT / "validate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for label, stats in all_stats.items():
        print(f"[{label}] 题目 {stats['total']} 道 | 应拒答 {stats['must_refuse']} 道 "
              f"| 题干已确认 {stats['questions_confirmed']} | 答案已标注 {stats['answers_labeled']}")
        print(f"  题型分布: {stats['type_distribution']}")
        print(f"  出处分布: {stats['source_doc_distribution']}")
        print(f"  答案逐字可核 {stats['answers_verbatim']} / 按 atoms 可核 "
              f"{stats['answers_atom_based']} / 块号已回填 {stats['source_chunk_filled']}")

    if errors:
        print(f"\n校验未通过，{len(errors)} 处问题：", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("\n校验通过：结构与出处可核性无问题（标注进度如上，未标完不算错误）")

    if args.strict and main_stats["answers_labeled"] < main_stats["total"]:
        print("--strict: 标准答案尚未标完，不能进入 D4", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
