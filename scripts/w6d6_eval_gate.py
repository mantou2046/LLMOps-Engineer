# -*- coding: utf-8 -*-
"""W6 D6：不调 LLM 的 eval 门禁（CI 用抽样子集，全量留本地周跑）。

用法（在作品集仓根目录）:
    python scripts/w6d6_eval_gate.py                    # CI 默认：20 题抽样子集
    python scripts/w6d6_eval_gate.py --subset 50       # 全量 38 道可答题
    python scripts/w6d6_eval_gate.py --self-test       # 正常放行 + 改坏 query 必须被拦
    python scripts/w6d6_eval_gate.py --sabotage        # 手动验证反例，应退出 1

门禁三件事，全程零网络、零模型、零第三方包：
    1. 数据完整性：题库 strict 校验、冻结语料 sha256、块号真实含答案、拒答题无出处。
    2. 检索回归：抽样子集用纯标准库 BM25 取 top-5，按块编号算 IDBasedContextRecall@5。
    3. 阈值：低于 evals/gate/config.json 的 threshold 就退出 1，CI 直接 fail。

「故意改坏 prompt」用 query_template.txt 复现：模板里的 {question} 被改掉或删除，
检索就拿不到题干，召回立刻掉破阈值。tests/test_w6d6_gate.py 把这个反例写成了单测。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w6d6_chunks import BM25Index, ROOT, build_chunks, load_golden, norm  # noqa: E402

GATE_DIR = ROOT / "evals" / "gate"
CONFIG = GATE_DIR / "config.json"
MAPPING = ROOT / "evals" / "corpus" / "source_chunks.json"
VALIDATOR = ROOT / "scripts" / "w6d3_validate_golden.py"
OUT = ROOT / "evals" / "out" / "w6d6"
SABOTAGE_QUERY = "xyzzy 无关占位查询 0000"


def log(msg: str) -> None:
    print(msg, flush=True)


def select_subset(rows: list[dict], size: int) -> list[dict]:
    """确定性分层抽样：先收齐难例，再按题号补 single_hop。"""
    answerable = [r for r in rows if not r.get("must_refuse")]
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in answerable:
        by_type[row["type"]].append(row)
    picked: list[dict] = []
    for kind in ("multi_hop", "boundary", "instruction"):
        picked.extend(by_type[kind])
    for row in by_type["single_hop"]:
        if len(picked) >= size:
            break
        picked.append(row)
    picked = picked[:size]
    return sorted(picked, key=lambda r: r["id"])


def data_checks(rows: list[dict], mapping: dict, chunks: list[dict]) -> list[str]:
    errors: list[str] = []
    by_id = {item["id"]: item for item in mapping.get("items", [])}
    if len(by_id) != len(rows):
        errors.append(f"source_chunks.json 只有 {len(by_id)} 题，题库有 {len(rows)} 题")

    for row in rows:
        rid = row["id"]
        item = by_id.get(rid)
        if item is None:
            errors.append(f"{rid} 缺块号映射")
            continue
        chunk = row.get("source_chunk")
        if row.get("must_refuse"):
            if chunk is not None or item.get("chunks"):
                errors.append(f"{rid} 是拒答题，不该有 source_chunk / 命中块")
            continue
        if not chunk:
            errors.append(f"{rid} 是可答题但 source_chunk 仍为空")
            continue
        try:
            idx = int(chunk.rsplit("#", 1)[1])
        except (IndexError, ValueError):
            errors.append(f"{rid} source_chunk 格式错: {chunk}")
            continue
        if not 0 <= idx < len(chunks):
            errors.append(f"{rid} 块号越界: {idx}")
            continue
        if idx not in item.get("chunks", []):
            errors.append(f"{rid} 主块 {idx} 不在命中块清单里")
        block = norm(chunks[idx]["text"])
        atoms = [norm(a) for a in (row.get("reference_atoms") or []) if norm(a)]
        if atoms and not any(a in block for a in atoms):
            errors.append(f"{rid} 主块 {idx} 里找不到任何一条标准答案要点")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--strict"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        errors.append(f"golden_set 严格校验失败（退出码 {result.returncode}）: "
                      f"{(result.stderr or result.stdout).strip()[:200]}")
    return errors


def run_gate(config: dict, subset: int, template_path: Path, sabotage: bool,
             write_report: bool = True) -> tuple[int, dict]:
    chunks = build_chunks()
    index = BM25Index(chunks)
    rows = load_golden()
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    errors = data_checks(rows, mapping, chunks)
    picked = select_subset(rows, subset)
    by_id = {item["id"]: item for item in mapping["items"]}
    template = template_path.read_text(encoding="utf-8").strip()
    top_k = int(config["top_k"])

    per_question = []
    for row in picked:
        query = SABOTAGE_QUERY if sabotage else template.format(question=row["question"])
        top = index.topk(query, top_k)
        expected = set(by_id[row["id"]].get("chunks", []))
        hit = bool(set(top) & expected)
        per_question.append({
            "id": row["id"], "type": row["type"], "hit": hit,
            "top5": top, "expected": sorted(expected),
        })
    hits = sum(1 for x in per_question if x["hit"])
    recall = round(hits / len(picked), 4) if picked else 0.0
    threshold = float(config["threshold"])
    passed = recall >= threshold and not errors
    try:
        tpl_label = str(template_path.relative_to(ROOT))
    except ValueError:
        tpl_label = str(template_path)  # 测试用 tmp_path 传坏模板，不在仓库内
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metric": "IDBasedContextRecall@5",
        "subset": len(picked), "top_k": top_k,
        "recall": recall, "hits": hits, "threshold": threshold,
        "sabotage": sabotage, "template": tpl_label,
        "data_errors": errors, "passed": passed,
        "per_question": per_question,
    }
    if write_report:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "gate_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return (0 if passed else 1), result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subset", type=int, default=0, help="抽样子集大小；0 = 用 config")
    ap.add_argument("--template", default="", help="覆盖 query 模板路径（测试改坏 prompt 用）")
    ap.add_argument("--sabotage", action="store_true", help="故意用无关 query，应退出 1")
    ap.add_argument("--self-test", action="store_true", help="正常放行 + 反例被拦，两者都验")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    subset = args.subset or int(config["subset_size"])
    template = (ROOT / args.template) if args.template else (ROOT / config["query_template"])

    if args.self_test:
        ok_code, ok = run_gate(config, subset, template, False, write_report=False)
        bad_code, bad = run_gate(config, subset, template, True, write_report=False)
        log(f"[self-test] 正常 recall={ok['recall']} exit={ok_code} | "
            f"改坏 query recall={bad['recall']} exit={bad_code}")
        run_gate(config, subset, template, False, write_report=True)
        passed = ok_code == 0 and bad_code == 1 and bad["recall"] < ok["recall"]
        log("[self-test] " + ("通过：正常放行、改坏被拦" if passed else "失败"))
        return 0 if passed else 1

    code, result = run_gate(config, subset, template, args.sabotage)
    tag = "[sabotage] " if args.sabotage else "[gate] "
    log(f"{tag}子集 {result['subset']} 题 | top-{result['top_k']} | "
        f"IDBasedContextRecall@5 = {result['recall']} (命中 {result['hits']}) | "
        f"阈值 {result['threshold']} | 数据检查 {len(result['data_errors'])} 错 | "
        f"{'PASS' if result['passed'] else 'FAIL'}")
    for err in result["data_errors"][:5]:
        log(f"  - {err}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
