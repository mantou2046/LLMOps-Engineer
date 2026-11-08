# -*- coding: utf-8 -*-
"""W6 D6：给 golden set 回填 source_chunk 块号，并产出可核的命中证据。

用法（在作品集仓根目录）:
    python scripts/w6d6_backfill_source_chunk.py --check      # 只算不写
    python scripts/w6d6_backfill_source_chunk.py --apply      # 写回 golden_set.jsonl

判据：
    - 切法 = W6 D1 的递归 B(400/50)，逐篇切、块不跨文档，全局块号 0..329。
      与 w6d1_chunk_bench.split_recursive 逐字对过，330 块一致。
    - 可答题：把 reference_atoms 的每条要点逐字核进块文本，取覆盖要点最多的块当主块；
      所有命中的块一起写进 evals/corpus/source_chunks.json，供 IDBasedContextRecall 用。
    - 逐字找不到时降到 20 字片段匹配并标记 fragment；仍找不到就留 null 标 unresolved，
      绝不凭空猜块号。
    - 拒答题没有出处，source_chunk 保持 null；门禁里不参与召回计分。

只用 Python 标准库。写回前会校验原 JSONL 可无损序列化，避免顺手改坏标注正本。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from w6d6_chunks import ROOT, build_chunks, norm  # noqa: E402

GOLDEN = ROOT / "evals" / "golden_set.jsonl"
MAPPING = ROOT / "evals" / "corpus" / "source_chunks.json"
REPORT = ROOT / "evals" / "SOURCE_CHUNK_BACKFILL.md"
FRAGMENT = 20


def load_rows() -> tuple[list[str], list[dict]]:
    lines = [l for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines]
    # 写回前确认序列化可逆，否则任何重写都会改动标注正本的其它字节。
    for lineno, (line, row) in enumerate(zip(lines, rows), 1):
        dumped = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if dumped != line:
            raise ValueError(f"golden_set.jsonl:{lineno} 序列化不可逆，拒绝写回")
    return lines, rows


def fragment_hits(atom: str, chunk_norms: list[str]) -> list[int]:
    """跨块被切断的要点，用 20 字片段找覆盖最多的块。"""
    s = norm(atom)
    frags = [s[i : i + FRAGMENT] for i in range(0, len(s), FRAGMENT)] if len(s) > FRAGMENT else [s]
    scored: list[tuple[int, int]] = []
    for idx, text in enumerate(chunk_norms):
        hits = sum(1 for f in frags if f and f in text)
        if hits:
            scored.append((hits, idx))
    if not scored:
        return []
    best = max(n for n, _ in scored)
    return [idx for n, idx in scored if n == best]


def locate(row: dict, chunk_norms: list[str]) -> dict:
    rid = row["id"]
    if row.get("must_refuse"):
        return {
            "id": rid, "source_chunk": None, "chunks": [], "method": "refuse",
            "atoms": [], "primary": None, "covered_atoms": 0, "total_atoms": 0,
        }

    atoms = row.get("reference_atoms") or [row.get("reference", "")]
    per_atom: list[dict] = []
    for atom in atoms:
        target = norm(atom)
        hits = [i for i, text in enumerate(chunk_norms) if target and target in text]
        method = "verbatim"
        if not hits:
            hits = fragment_hits(atom, chunk_norms)
            method = "fragment" if hits else "unresolved"
        per_atom.append({"method": method, "hits": hits, "excerpt": target[:80]})

    cover: Counter = Counter()
    for item in per_atom:
        for idx in set(item["hits"]):
            cover[idx] += 1
    all_hits = sorted({idx for item in per_atom for idx in item["hits"]})
    if cover:
        primary = min(cover, key=lambda i: (-cover[i], i))
        method = (
            "verbatim" if all(a["method"] == "verbatim" for a in per_atom)
            else "fragment" if all(a["method"] != "unresolved" for a in per_atom)
            else "partial"
        )
    else:
        primary, method = None, "unresolved"
    return {
        "id": rid,
        "primary": primary,
        "chunks": all_hits,
        "covered_atoms": max(cover.values()) if cover else 0,
        "total_atoms": len(atoms),
        "method": method,
        "atoms": per_atom,
    }


def build_report(results: list[dict], chunks: list[dict], applied: bool) -> str:
    total = len(results)
    refuse = [r for r in results if r["method"] == "refuse"]
    answerable = [r for r in results if r["method"] != "refuse"]
    counts = Counter(r["method"] for r in answerable)
    full = sum(1 for r in answerable if r["covered_atoms"] == r["total_atoms"])
    unresolved = [r for r in answerable if r["method"] in ("unresolved", "partial", "fragment")]
    lines = [
        "# W6 D6 · golden set 的 source_chunk 块号回填",
        "",
        f"> {date.today().isoformat()} 实跑。切法与 W6 D1 的递归 B(400/50) 逐字对账："
        f"六篇冻结语料 104,798 字符 → **{len(chunks)} 块**，全局块号 0..{len(chunks) - 1}。",
        "",
        "## 口径",
        "",
        "`source_chunk` 形如 `k8s-concepts.md#84`，前半是冻结语料文件名，后半是**全局块号**，",
        "与 `scripts/w6d1_chunk_bench.py` / `w6d4_rag_chain.py` 里的 `chunk_indices` 同一套编号。",
        "判据是把 `reference_atoms` 的每条要点逐字核进块文本，取覆盖要点最多的块当主块；",
        "所有命中的块另存 `evals/corpus/source_chunks.json`，给 `IDBasedContextRecall` 用。",
        "逐字找不到才降到 20 字片段匹配；仍找不到就留 `null`，不猜。",
        "",
        "## 结果",
        "",
        "| 项 | 数量 |",
        "| --- | --- |",
        f"| 题目总数 | {total} |",
        f"| 可答题 | {len(answerable)} |",
        f"| 拒答题（保持 null） | {len(refuse)} |",
        f"| 逐字全部命中 | {counts.get('verbatim', 0)} |",
        f"| 片段兜底 / 部分命中 / 未定位 | {counts.get('fragment', 0)} / "
        f"{counts.get('partial', 0)} / {counts.get('unresolved', 0)} |",
        f"| 所有要点都落在同一批块里 | {full}/{len(answerable)} |",
        f"| 已写入 golden_set.jsonl | {'是' if applied else '否（--check）'} |",
        "",
        "## 需要人工复核的题",
        "",
    ]
    if unresolved:
        lines.append("| 题号 | 方法 | 命中要点 / 总数 | 命中块数 |")
        lines.append("| --- | --- | --- | --- |")
        for r in unresolved:
            lines.append(
                f"| {r['id']} | {r['method']} | {r['covered_atoms']}/{r['total_atoms']} "
                f"| {len(r['chunks'])} |"
            )
    else:
        lines.append("无。所有可答题的每条要点都逐字核进了块文本。")
    lines += [
        "",
        "## 复跑",
        "",
        "```bash",
        "cd /e/Projects/LLMOps-Engineer",
        "python scripts/w6d6_backfill_source_chunk.py --check",
        "python scripts/w6d6_backfill_source_chunk.py --apply",
        "python scripts/w6d3_validate_golden.py --strict",
        "```",
        "",
        "改动的是 `source_chunk` 一列，其余字段逐字节不动；写回前会校验 JSONL 序列化可逆。",
        "语料按 `evals/corpus/manifest.json` 的 sha256 逐个核对，换语料要先重冻快照再重跑。",
        "",
        "#LLMOps #RAG #评估",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="只算不写")
    group.add_argument("--apply", action="store_true", help="写回题库与映射")
    args = ap.parse_args()

    _, rows = load_rows()
    chunks = build_chunks()
    chunk_norms = [norm(c["text"]) for c in chunks]
    results = [locate(row, chunk_norms) for row in rows]
    for row, result in zip(rows, results):
        result["source_chunk"] = (
            chunks[result["primary"]]["source_chunk"] if result.get("primary") is not None else None
        )

    counts = Counter(r["method"] for r in results)
    print(f"[chunks] {len(chunks)} 块 / 6 篇 | 判据 = reference_atoms 逐字核块")
    print(f"[result] " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"[filled] {sum(1 for r in results if r['source_chunk'])} / {len(results)}")

    if args.apply:
        for row, result in zip(rows, results):
            row["source_chunk"] = result["source_chunk"]
        GOLDEN.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n",
            encoding="utf-8",
        )
        MAPPING.write_text(
            json.dumps(
                {
                    "purpose": "source_chunk 的完整命中块清单；IDBasedContextRecall 与 CI 门禁用",
                    "split": "recursive_B(400/50)",
                    "total_chunks": len(chunks),
                    "generated_at": date.today().isoformat(),
                    "items": [
                        {k: r[k] for k in ("id", "source_chunk", "chunks", "method",
                                           "covered_atoms", "total_atoms")}
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(f"[write] {GOLDEN.relative_to(ROOT)} / {MAPPING.relative_to(ROOT)}")

    REPORT.write_text(build_report(results, chunks, args.apply), encoding="utf-8")
    print(f"[write] {REPORT.relative_to(ROOT)}")
    bad = [r["id"] for r in results if r["method"] == "unresolved"]
    if bad:
        print(f"[warn] 未定位题号: {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
