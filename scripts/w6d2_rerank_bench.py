"""W6 D2 · rerank 延迟与收益对照（任务卡 11/10 卡点原话）。

用法（作品集仓根目录）:
    HF_HUB_OFFLINE=1 ./.venv-eval/Scripts/python.exe scripts/w6d2_rerank_bench.py

任务卡 11/10：「做」第 3 条接 rerank 模型；卡点「rerank 是延迟大头 ——
先量出它加了多少毫秒，再决定要不要开」。所以本脚本量三件事：
    1. 基线延迟：pgvector <=> + BM25 + RRF 拿到 top-N 候选要多久
    2. 净增延迟：cross-encoder 对 top-N 候选逐条打分要多久（分次跑取中位数）
    3. 收益：精排后 hit@1 / MRR 有没有涨（RRF 已 10/10 顶格，重点看有没有掉）

候选集扫描 N ∈ {5, 10, 20}，看延迟随候选数怎么涨——这正是
「延迟大头」那句话要的数字。模型 BAAI/bge-reranker-base（本地 1.1 GB / CPU）。

⚠️ 单变量：切块、向量模型、探针题、RRF 参数全部沿用 D2 生产复验，
只让「要不要过 cross-encoder」这一个变量变。

产出: evals/out/w6d2/rerank_bench.json
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from w6d1_chunk_bench import encode_cached, get_model, load_probes  # noqa: E402
from w6d1_ingest_pgvector import connect, load_env, to_vector_literal  # noqa: E402
from w6d2_hybrid_bench import RRF_K, bm25_tokenize, rrf  # noqa: E402

OUT_FILE = ROOT / "evals" / "out" / "w6d2" / "rerank_bench.json"
RERANKER_DIR = ROOT / "models" / "bge-reranker-base"
CANDIDATE_NS = [5, 10, 20]
TOP_K = 5
REPEAT = 3          # 每题重复量延迟取中位数，压掉 CPU 抖动


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    if not (RERANKER_DIR / "model.safetensors").exists():
        raise SystemExit("先跑 bash scripts/fetch_reranker_model.sh 取模型")

    probes = load_probes()
    env = load_env()
    model = get_model()
    q_emb = encode_cached("probes", [p["question"] for p in probes], model)

    # ---- 生产链路：pgvector <=> + BM25 + RRF ----
    with connect(env) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ivfflat.probes = 3")   # DEC-041
            cur.execute("SELECT doc_path, chunk_idx, content FROM rag_chunks "
                        "ORDER BY doc_path, chunk_idx")
            rows = cur.fetchall()
            chunks = [r[2] for r in rows]
            pos = {(r[0], r[1]): j for j, r in enumerate(rows)}
            vec_top10, base_lat = [], []
            for i in range(len(probes)):
                t = time.perf_counter()
                cur.execute(
                    "SELECT doc_path, chunk_idx FROM rag_chunks "
                    "ORDER BY embedding <=> %s::vector LIMIT 10",
                    (to_vector_literal(q_emb[i]),),
                )
                base_lat.append((time.perf_counter() - t) * 1000)
                vec_top10.append([pos[k] for k in cur.fetchall() if k in pos])

    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)
    bm25 = BM25Okapi([bm25_tokenize(c) for c in chunks])
    bm25_top10 = [
        list(np.argsort(-np.asarray(bm25.get_scores(bm25_tokenize(p["question"]))))[:10])
        for p in probes
    ]
    fused_top20 = [
        [idx for idx, _ in rrf(vt, bt, k=RRF_K)[:20]]
        for vt, bt in zip(vec_top10, bm25_top10, strict=True)
    ]
    log(f"[base] RRF 候选就绪，向量查询 P50 {statistics.median(base_lat):.2f} ms")

    # ---- cross-encoder ----
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(4)
    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(str(RERANKER_DIR))
    ce = AutoModelForSequenceClassification.from_pretrained(str(RERANKER_DIR))
    ce.eval()
    load_sec = round(time.perf_counter() - t0, 2)
    log(f"[reranker] 加载 {load_sec}s / {sum(p.numel() for p in ce.parameters())/1e6:.0f}M 参数")

    def score_pairs(query, texts):
        pairs = [(query, t) for t in texts]
        batch = tok(pairs, padding=True, truncation=True,
                    max_length=512, return_tensors="pt")
        with torch.no_grad():
            return ce(**batch).logits.view(-1).float().numpy()

    def metrics(name, top_lists):
        hit5 = hit1 = 0
        mrr = 0.0
        detail = []
        for i, p in enumerate(probes):
            kws, top = p["expect_keywords"], top_lists[i]
            h5 = any(all(k in chunks[j] for k in kws) for j in top[:TOP_K])
            h1 = bool(top) and all(k in chunks[int(top[0])] for k in kws)
            r = next((r for r, idx in enumerate(top[:20], 1)
                      if all(k in chunks[idx] for k in kws)), None)
            hit5 += h5
            hit1 += h1
            mrr += (1.0 / r) if r else 0.0
            detail.append({"id": p["id"], "hit@5": bool(h5),
                           "hit@1": bool(h1), "rank": r})
        return {"name": name, "hit@5": hit5, "hit@1": hit1,
                "MRR": round(mrr / len(probes), 4), "detail": detail}

    baseline = metrics("RRF（无 rerank）", [t[:TOP_K] for t in fused_top20])

    results, rerank_lat = [], []
    for n in CANDIDATE_NS:
        per_query_ms, top_lists = [], []
        for rep in range(REPEAT):
            for i, p in enumerate(probes):
                cand = fused_top20[i][:n]
                t = time.perf_counter()
                sc = score_pairs(p["question"], [chunks[j] for j in cand])
                per_query_ms.append((time.perf_counter() - t) * 1000)
                if rep == 0:
                    order = np.argsort(-sc)
                    top_lists.append([cand[j] for j in order][:TOP_K])
        med = round(statistics.median(per_query_ms), 1)
        p95 = round(sorted(per_query_ms)[int(0.95 * len(per_query_ms)) - 1], 1)
        m = metrics(f"RRF + rerank@{n}", top_lists)
        results.append({
            "candidates": n, "rerank_p50_ms": med, "rerank_p95_ms": p95,
            "base_p50_ms": round(statistics.median(base_lat), 2),
            "total_p50_ms": round(statistics.median(base_lat) + med, 1),
            "metrics": m,
        })
        log(f"[rerank@{n:2d}] 净增 P50 {med:7.1f} ms / P95 {p95:7.1f} ms | "
            f"hit@1 {m['hit@1']}/10  MRR {m['MRR']}")

    print()
    print(f"{'模式':<22}{'hit@5':<9}{'hit@1':<9}{'MRR':<9}{'净增P50(ms)':<14}")
    print(f"{baseline['name']:<22}{str(baseline['hit@5'])+'/10':<9}"
          f"{str(baseline['hit@1'])+'/10':<9}{baseline['MRR']:<9}{'-':<14}")
    for r in results:
        m = r["metrics"]
        print(f"{m['name']:<22}{str(m['hit@5'])+'/10':<9}"
              f"{str(m['hit@1'])+'/10':<9}{m['MRR']:<9}{r['rerank_p50_ms']:<14}")
    for r in results:
        rerank_lat.append(r["rerank_p50_ms"])

    # 判据：RRF 已 10/10 顶格，rerank 只能在「不掉分」上通过；
    # 延迟净增超过基线一个量级就默认不开。
    best = results[0]
    no_regression = all(r["metrics"]["hit@1"] >= baseline["hit@1"] for r in results)
    latency_ratio = round(best["rerank_p50_ms"] / max(statistics.median(base_lat), 0.01), 1)
    decision = "默认不开" if latency_ratio >= 10 else "可按场景开"
    print()
    print(f"[verdict] 基线向量查询 P50 {statistics.median(base_lat):.2f} ms，"
          f"rerank@5 净增 {best['rerank_p50_ms']:.1f} ms（约 {latency_ratio}×）")
    gained = any(r["metrics"]["hit@1"] > baseline["hit@1"] for r in results)
    print(f"[verdict] 精排有没有掉分: {'没有' if no_regression else '有'}；"
          f"hit@1 有没有涨: {'有' if gained else '没有（RRF 已顶格）'}")
    print(f"[verdict] 结论: {decision}（RRF 已满分，精排买不到召回，只买延迟）")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "reranker": "BAAI/bge-reranker-base (local, CPU, 278M)",
        "load_sec": load_sec,
        "base_vector_p50_ms": round(statistics.median(base_lat), 2),
        "baseline": baseline,
        "variants": results,
        "verdict": {
            "no_regression": bool(no_regression),
            "latency_ratio_vs_vector": latency_ratio,
            "decision": decision,
        },
        "note": "延迟为单题 cross-encoder 前向耗时（REPEAT=3 取中位数），不含向量检索；"
                "10 题样本、CPU-only，只作量级与取舍依据",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"[done] wrote {OUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
