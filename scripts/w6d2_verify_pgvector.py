"""W6 D2 · 在生产库 pgvector 上复跑混合检索（手册「四、下一步」第 1 条）。

用法（作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d2_verify_pgvector.py

为什么需要这一步：11/5 上午的 w6d2_hybrid_bench.py 是离线内存实验，
向量路用 numpy 内积、BM25 路用内存里的块，没走真索引。11/5 下午 D1
把 rag_chunks 灌成 330 行 vector(512) 之后，才具备在生产链路上复核的条件。

三路对照（同一套 evals/chunk_probe_set.jsonl 10 题）:
    vec  : pgvector <=> 余弦距离，显式 SET ivfflat.probes = 3（DEC-041）
    bm25 : jieba + BM25Okapi，语料直接取 rag_chunks.content
    rrf  : 两路各取 top-10，按 1/(60+rank) 融合

对照离线基线 evals/out/w6d2/results.json：
    vec hit@1 = 7/10 / MRR 0.825，rrf hit@1 = 10/10 / MRR 1.0
掉分先隔离 ANN（SET enable_indexscan = off），别动融合结论。

产出: evals/out/w6d2/pgvector_verify.json
"""
from __future__ import annotations

import json
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from w6d1_chunk_bench import encode_cached, get_model, load_probes  # noqa: E402
from w6d1_ingest_pgvector import connect, load_env, to_vector_literal  # noqa: E402
from w6d2_hybrid_bench import RRF_K, TOK_TOPK, bm25_tokenize, rrf  # noqa: E402

OUT_FILE = ROOT / "evals" / "out" / "w6d2" / "pgvector_verify.json"
OFFLINE = ROOT / "evals" / "out" / "w6d2" / "results.json"


def evaluate(name, top_lists, chunks, probes):
    detail, hit5, hit1, mrr_sum = [], 0, 0, 0.0
    for i, p in enumerate(probes):
        kws, top = p["expect_keywords"], top_lists[i]
        h5 = any(all(k in chunks[j] for k in kws) for j in top[:5])
        h1 = bool(top) and all(k in chunks[int(top[0])] for k in kws)
        rank = next((r for r, idx in enumerate(top[:TOK_TOPK], 1)
                     if all(k in chunks[idx] for k in kws)), None)
        hit5 += h5
        hit1 += h1
        mrr_sum += (1.0 / rank) if rank else 0.0
        detail.append({"id": p["id"], "hit@5": bool(h5), "hit@1": bool(h1), "rank": rank})
    return {
        "name": name, "hit@5": hit5, "hit@1": hit1,
        "MRR": round(mrr_sum / len(probes), 4), "probes": len(probes),
        "detail": detail,
    }


def main() -> int:
    probes = load_probes()
    model = get_model()
    q_emb = encode_cached("probes", [p["question"] for p in probes], model)
    env = load_env()

    t0 = time.perf_counter()
    with connect(env) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ivfflat.probes = 3")  # DEC-041：默认 1 会掉分且不稳定
            cur.execute(
                "SELECT doc_path, chunk_idx, content FROM rag_chunks "
                "ORDER BY doc_path, chunk_idx"
            )
            rows = cur.fetchall()
            chunks = [r[2] for r in rows]
            vec_top, latencies = [], []
            for i in range(len(probes)):
                lit = to_vector_literal(q_emb[i])
                t = time.perf_counter()
                cur.execute(
                    "SELECT doc_path, chunk_idx FROM rag_chunks "
                    f"ORDER BY embedding <=> %s::vector LIMIT {TOK_TOPK}",
                    (lit,),
                )
                latencies.append((time.perf_counter() - t) * 1000)
                keys = [(r[0], r[1]) for r in cur.fetchall()]
                pos = {(r[0], r[1]): j for j, r in enumerate(rows)}
                vec_top.append([pos[k] for k in keys if k in pos])
    db_sec = round(time.perf_counter() - t0, 2)
    print(f"[db] rag_chunks {len(chunks)} 行 / vector 512；"
          f"向量检索 P50 {np.median(latencies):.2f} ms / {len(probes)} 题共 {db_sec}s")

    # BM25 路：语料取生产库里的 content，与离线实验同一个分词口径
    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)
    bm25 = BM25Okapi([bm25_tokenize(c) for c in chunks])
    bm25_top = [
        list(np.argsort(-np.asarray(bm25.get_scores(bm25_tokenize(p["question"]))))[:TOK_TOPK])
        for p in probes
    ]
    fused_top = [
        [idx for idx, _ in rrf(vt, bt, k=RRF_K)[:TOK_TOPK]]
        for vt, bt in zip(vec_top, bm25_top, strict=True)
    ]

    methods = OrderedDict()
    methods["vec"] = evaluate("向量单路（pgvector <=>）", vec_top, chunks, probes)
    methods["bm25"] = evaluate("BM25 单路（jieba）", bm25_top, chunks, probes)
    methods["rrf"] = evaluate("RRF 融合（k=60）", fused_top, chunks, probes)

    offline = {m["name"].split("（")[0]: m for m in
               json.loads(OFFLINE.read_text(encoding="utf-8"))["methods"]}
    # 离线 JSON 里的键名是中文全称，按前缀映射
    off_vec = next(m for m in offline.values() if m["name"].startswith("向量"))
    off_rrf = next(m for m in offline.values() if m["name"].startswith("RRF"))

    print()
    print(f"{'检索路':<28}{'hit@5':<9}{'hit@1':<9}{'MRR':<9}")
    for m in methods.values():
        print(f"{m['name']:<28}{str(m['hit@5'])+'/10':<9}"
              f"{str(m['hit@1'])+'/10':<9}{m['MRR']:<9}")
    print(f"{'离线基线 vec':<28}{str(off_vec['hit_at5'])+'/10':<9}"
          f"{str(off_vec['hit_at1'])+'/10':<9}{off_vec['mrr']:<9}")
    print(f"{'离线基线 rrf':<28}{str(off_rrf['hit_at5'])+'/10':<9}"
          f"{str(off_rrf['hit_at1'])+'/10':<9}{off_rrf['mrr']:<9}")

    vec, rrf_m = methods["vec"], methods["rrf"]
    match = (vec["hit@5"] == off_vec["hit_at5"] and vec["hit@1"] == off_vec["hit_at1"]
             and rrf_m["hit@5"] == off_rrf["hit_at5"]
             and rrf_m["hit@1"] == off_rrf["hit_at1"])
    improved = rrf_m["hit@1"] > vec["hit@1"] and rrf_m["MRR"] > vec["MRR"]
    print()
    print(f"[verdict] 与离线基线逐项对上: {'是' if match else '否（先隔离 ANN，别动融合结论）'}")
    print(f"[verdict] 融合优于向量单路: {'是' if improved else '否'}")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "db": {"rows": len(chunks), "dim": 512, "ivfflat_probes": 3},
        "vec_query_p50_ms": round(float(np.median(latencies)), 2),
        "offline_baseline": {
            "vec": {"hit@5": off_vec["hit_at5"], "hit@1": off_vec["hit_at1"],
                    "MRR": off_vec["mrr"]},
            "rrf": {"hit@5": off_rrf["hit_at5"], "hit@1": off_rrf["hit_at1"],
                    "MRR": off_rrf["mrr"]},
        },
        "methods": list(methods.values()),
        "verdict_match": bool(match),
        "verdict_improved": bool(improved),
        "note": "BM25 路语料取自 rag_chunks.content；rerank 未接入（见 w6d2_rerank_bench.py）",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] wrote {OUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
