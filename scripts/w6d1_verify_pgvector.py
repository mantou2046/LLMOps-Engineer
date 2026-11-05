# -*- coding: utf-8 -*-
"""W6 D1 · 11/9 灌库后复验清单的可复跑实现（手册「四、下一步」6 条）。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d1_verify_pgvector.py

对照 D1 离线基线（evals/out/w6d1/results.json，递归 B）:
    chunks = 330, hit@5 = 10/10, hit@1 = 7/10

跑四组:
    exact  : SET enable_indexscan=off / enable_bitmapscan=off -> 强制顺序精确检索
    ann    : 默认规划器（IVFFlat, lists=10, ivfflat.probes=1）
    ann_p3 : 同上但 SET ivfflat.probes=3（看 ANN 掉分是不是 probes 太少）
    每组报 hit@5 / hit@1 / MRR / 单次查询 P50 P95

判据（手册复验清单第 5 条）: 数字对得上，才把「递归 B 400/50」写成正式基线；
掉分先隔离 ANN，仍不一致查 embedding 是否被改写，别改切法结论。
BM25 那一路留给 D2，本脚本不接（一次只改一个变量）。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from w6d1_chunk_bench import encode_cached, get_model, load_probes  # noqa: E402
from w6d1_ingest_pgvector import connect, load_env, to_vector_literal  # noqa: E402

OUT_FILE = ROOT / "evals" / "out" / "w6d1" / "pgvector_verify.json"
TOP_K = 5
PROBE_FILE = ROOT / "evals" / "chunk_probe_set.jsonl"


def run_group(conn, q_vecs, probes, label: str, setup: list[str]):
    latencies, details = [], []
    with conn.cursor() as cur:
        for stmt in setup:
            cur.execute(stmt)
        for i, p in enumerate(probes):
            kws = p["expect_keywords"]
            t0 = time.perf_counter()
            cur.execute(
                """
                SELECT content, embedding <=> %s::vector AS dist
                FROM rag_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (to_vector_literal(q_vecs[i]), to_vector_literal(q_vecs[i]), TOP_K),
            )
            rows = cur.fetchall()
            latencies.append((time.perf_counter() - t0) * 1000)
            hit5 = any(all(k in c for k in kws) for c, _ in rows)
            hit1 = bool(rows) and all(k in rows[0][0] for k in kws)
            rank = next((j + 1 for j, (c, _) in enumerate(rows)
                         if all(k in c for k in kws)), 0)
            details.append({
                "id": p["id"], "keywords": kws, "hit@5": hit5,
                "hit@1": hit1, "rank": rank,
                "top1_dist": round(float(rows[0][1]), 5) if rows else None,
            })
        # 复位会话级 SET，避免污染下一组
        cur.execute("RESET ALL")

    lat = sorted(latencies)
    p50 = round(statistics.median(lat), 2)
    p95 = round(lat[min(len(lat) - 1, int(np.ceil(0.95 * len(lat))) - 1)], 2)
    mrr = round(sum(1.0 / d["rank"] for d in details if d["rank"]) / len(details), 4)
    return {
        "label": label,
        "hit@5": sum(d["hit@5"] for d in details),
        "hit@1": sum(d["hit@1"] for d in details),
        "MRR": mrr,
        "query_p50_ms": p50,
        "query_p95_ms": p95,
        "probes_count": len(probes),
        "details": details,
    }


def main() -> int:
    probes = load_probes()
    model = get_model()
    q_emb = encode_cached("probes", [p["question"] for p in probes], model)
    if q_emb.shape[1] != 512:
        raise SystemExit(f"探针向量维度不对: {q_emb.shape[1]}")

    env = load_env()
    groups = []
    with connect(env) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), max(vector_dims(embedding)) FROM rag_chunks")
            rows, dim = cur.fetchone()
            cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname='idx_rag_chunks_emb'")
            idx = cur.fetchone()
        print(f"[db] rag_chunks {rows} 行 / vector({dim})")
        print(f"[db] 索引: {idx[0] if idx else '(无)'}")

        groups.append(run_group(conn, q_emb, probes, "exact（顺序精确检索）", [
            "SET enable_indexscan = off", "SET enable_bitmapscan = off",
        ]))
        groups.append(run_group(conn, q_emb, probes, "ann（IVFFlat, probes=1）", [
            "SET ivfflat.probes = 1",
        ]))
        groups.append(run_group(conn, q_emb, probes, "ann（IVFFlat, probes=3）", [
            "SET ivfflat.probes = 3",
        ]))

    # 离线基线
    baseline_file = ROOT / "evals" / "out" / "w6d1" / "results.json"
    base_rows = json.loads(baseline_file.read_text(encoding="utf-8"))["rows"]
    base_B = next(r for r in base_rows if r["key"] == "rec_B")

    print()
    print(f"{'组':<28}{'hit@5':<9}{'hit@1':<9}{'MRR':<9}{'P50ms':<9}{'P95ms':<9}")
    for g in groups:
        print(f"{g['label']:<28}{str(g['hit@5'])+'/10':<9}{str(g['hit@1'])+'/10':<9}"
              f"{g['MRR']:<9}{g['query_p50_ms']:<9}{g['query_p95_ms']:<9}")
    print(f"{'离线基线（D1 递归 B）':<28}{str(base_B['hit_at5'])+'/10':<9}"
          f"{str(base_B['hit_at1'])+'/10':<9}{'-':<9}{'-':<9}{'-':<9}")

    exact = groups[0]
    match = (rows == base_B["chunks"] and dim == 512
             and exact["hit@5"] == base_B["hit_at5"]
             and exact["hit@1"] == base_B["hit_at1"])
    print()
    print(f"[verdict] 行数 {rows} vs {base_B['chunks']}，精确检索 hit@5 "
          f"{exact['hit@5']} vs {base_B['hit_at5']}，hit@1 {exact['hit@1']} vs {base_B['hit_at1']}")
    verdict = "是，可写正式基线" if match else "否，先查环境别改切法结论"
    print(f"[verdict] 离线结论能否搬到生产: {verdict}")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "db": {"rows": rows, "dim": dim, "index": idx[0] if idx else None},
        "offline_baseline": {
            "chunks": base_B["chunks"], "hit@5": base_B["hit_at5"],
            "hit@1": base_B["hit_at1"], "source": "evals/out/w6d1/results.json",
        },
        "groups": groups,
        "verdict_pass": bool(match),
        "note": "BM25 未接入（留 D2）；probe_set 与 D1 同为 evals/chunk_probe_set.jsonl",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] wrote {OUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
