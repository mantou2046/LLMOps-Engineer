# -*- coding: utf-8 -*-
"""W6 D2 动手实践：混合检索（BM25 + 向量）与 RRF 融合的实跑基准。

复用 w6d1 的切块（递归 B 400/50，已是 D1 选定的生产基线切法）、
本地 bge-small-zh-v1.5 向量模型、以及 docs/*.md 语料与 chunk_probe_set.jsonl 探针。

本脚本新增两条检索路与融合：
  - 向量路：问题 / 块经 bge 编码后余弦取 top-k（与 D1 同口径）。
  - 关键词路（BM25）：jieba 分词后建 BM25Okapi，问题同样分词取 top-k。
  - RRF 融合：两路各自的排名按 1/(k+rank) 累加（Elastic / LlamaIndex 通用写法，k=60）。

判据沿用 D1：命中 = top-5 中至少一块包含该题 expect_keywords 全部关键词。
额外报告 hit@1 与 MRR，用来在向量单路 hit@5 已顶格（10/10）时
仍能看出融合是否带来提升——这正是 11/10 验收「融合后命中率更高」的诚实口径。

用法（作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d2_hybrid_bench.py

产出:
    evals/out/w6d2/results.json
"""
from __future__ import annotations

import json
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from w6d1_chunk_bench import (  # noqa: E402
    ROOT as _R,
    load_docs,
    load_probes,
    get_model,
    encode_cached,
    split_recursive,
    EVALS_DIR,
)

EVALS_DIR = EVALS_DIR
OUT_DIR = EVALS_DIR / "out" / "w6d2"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TOK_TOPK = 10
RRF_K = 60


def log(msg: str) -> None:
    print(msg, flush=True)


def bm25_tokenize(text: str):
    import jieba

    return [t for t in jieba.lcut(text) if t.strip()]


def rrf(*rank_lists, k: int = RRF_K):
    """rank_lists: 每个元素是「文档索引按相关性从高到低的列表」。返回融合后 (idx, score) 降序。"""
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def main():
    docs = load_docs()
    probes = load_probes()
    corpus_chars = sum(len(t) for _, t in docs)
    log(f"[corpus] {len(docs)} docs, {corpus_chars} chars, {len(probes)} probes")

    # 切块：递归 B（D1 选定基线）
    chunks = []
    t0 = time.time()
    for _, text in docs:
        chunks.extend(split_recursive(text, 400, 50))
    chunks = [c for c in chunks if c.strip()]
    log(f"[split] recursive_B(400/50): {len(chunks)} chunks in {time.time() - t0:.1f}s")

    model = get_model()

    # ---- 向量路 ----
    c_emb = encode_cached("chunks_rec_B", chunks, model)  # 复用 D1 已算好的块向量
    q_emb = encode_cached("probes", [p["question"] for p in probes], model)
    vec_scores = q_emb @ c_emb.T  # (nq, nchunks)
    vec_top = [list(np.argsort(-vec_scores[i])[:TOK_TOPK]) for i in range(len(probes))]

    # ---- BM25 路 ----
    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)  # 静默 jieba 的 Building prefix dict 等提示
    t0 = time.time()
    corpus_tokens = [bm25_tokenize(c) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)
    bm25_top = []
    for p in probes:
        q_tokens = bm25_tokenize(p["question"])
        sc = bm25.get_scores(q_tokens)
        bm25_top.append(list(np.argsort(-np.asarray(sc))[:TOK_TOPK]))
    log(f"[bm25] built + scored {len(probes)} queries in {time.time() - t0:.1f}s")

    # ---- RRF 融合 ----
    fused_top = []
    for vt, bt in zip(vec_top, bm25_top):
        fused = rrf(vt, bt, k=RRF_K)
        fused_top.append([idx for idx, _ in fused[:TOK_TOPK]])

    # ---- 评估 ----
    def per_method(name, top_lists):
        detail, hit5, hit1, mrr_sum = [], 0, 0, 0.0
        for i, p in enumerate(probes):
            kws = p["expect_keywords"]
            top = top_lists[i]
            h5 = any(all(k in chunks[j] for k in kws) for j in top[:5])
            h1 = all(k in chunks[int(top[0])] for k in kws)
            # MRR：在前 TOK_TOPK 内第一个命中的排名
            mrr_rank = None
            for r, idx in enumerate(top[:TOK_TOPK], start=1):
                if all(k in chunks[idx] for k in kws):
                    mrr_rank = r
                    break
            hit5 += h5
            hit1 += h1
            mrr_sum += (1.0 / mrr_rank) if mrr_rank else 0.0
            detail.append(
                {
                    "id": p["id"],
                    "keywords": kws,
                    "hit": bool(h5),
                    "hit_at1": bool(h1),
                    "mrr_rank": mrr_rank,
                }
            )
        return {
            "name": name,
            "hit_at5": hit5,
            "hit_at1": hit1,
            "mrr": round(mrr_sum / len(probes), 4),
            "probes": len(probes),
            "detail": detail,
        }

    methods = OrderedDict()
    methods["vec"] = per_method("向量单路（bge 余弦）", vec_top)
    methods["bm25"] = per_method("BM25 单路（jieba 分词）", bm25_top)
    methods["rrf"] = per_method("RRF 融合（k=60）", fused_top)

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "corpus": {"dir": "docs/*.md", "files": [n for n, _ in docs], "chars": corpus_chars},
        "chunks": len(chunks),
        "chunker": "recursive_B(400/50)",
        "model": "models/bge-small-zh-v1.5 (512-dim, cosine)",
        "probe_set": "evals/chunk_probe_set.jsonl",
        "rrf_k": RRF_K,
        "metric_notes": {
            "hit_at5": "top-5 中至少一块含全部 expect_keywords",
            "hit_at1": "top-1 块含全部 expect_keywords",
            "mrr": "前 10 名内首个命中的 1/rank 均值；未命中记 0",
        },
        "methods": list(methods.values()),
    }
    out = OUT_DIR / "results.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for m in methods.values():
        log(
            "[result] {:<22} hit@5={}/{} hit@1={}/{} MRR={}".format(
                m["name"], m["hit_at5"], m["probes"], m["hit_at1"], m["probes"], m["mrr"]
            )
        )
    log(f"[done] wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
