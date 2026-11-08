"""W6 D2 · 按 DEC-042 复核条件重评 rerank（2026-11-08 晚间）。

DEC-042 原话：默认不开 rerank，等 11/11 的 50 题 golden set 与 11/12 的 RAGAS
四项出来再重评。当时用关键词判据（rerank 后 hit@1 掉到 6–7/10），报告自己承认
那把尺子量不了语义排序。本次改用带 reference 的
``LLMContextPrecisionWithReference``（独立裁判 glm-5.3-flash）比较
「RRF top-5」与「rerank 重排 top-10 → top-5」，一次只改排序这一个变量。

用法（作品集仓根目录；judge 通道走环境变量，脚本不读 .env、不落盘凭据）：
    ./.venv-eval/Scripts/python.exe scripts/w6d2_rerank_recheck.py \
        --judge glm-5.3-flash --batch-size 4
产出：evals/out/w6d2/rerank_recheck.json（该目录被 gitignore）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w6d1_chunk_bench import (  # noqa: E402
    encode_cached,
    get_model,
    load_docs,
    split_recursive,
)
from w6d2_hybrid_bench import RRF_K, TOK_TOPK, bm25_tokenize, rrf  # noqa: E402
from w6d4_rag_chain import CHUNK_OVERLAP, CHUNK_SIZE, TOP_K  # noqa: E402
from w6d4_ragas_eval import build_evaluator, to_records  # noqa: E402

GOLDEN = ROOT / "evals/golden_set.jsonl"
RESPONSES = ROOT / "evals/out/w6d4/responses.jsonl"
RESOURCE = ROOT / "models/bge-reranker-base"
OUT = ROOT / "evals/out/w6d2/rerank_recheck.json"
CP_KEY = "llm_context_precision_with_reference"

# 分层抽样：只取可答题（refuse 的 reference 写的是期望行为，CP 不适用）。
SAMPLE = {"single_hop": 11, "multi_hop": 3, "instruction": 3, "boundary": 3}


def log(msg: str) -> None:
    print(msg, flush=True)


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 6)


def pick_rows() -> list[dict]:
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    picked: list[dict] = []
    for kind, count in SAMPLE.items():
        group = [r for r in rows if r.get("type") == kind and not r.get("must_refuse")]
        picked.extend(group[:count])
    return sorted(picked, key=lambda r: r["id"])


def build_candidates(questions: list[str]) -> tuple[list[str], list[list[int]]]:
    docs = load_docs()
    chunks: list[str] = []
    for _name, text in docs:
        chunks.extend([c for c in split_recursive(text, CHUNK_SIZE, CHUNK_OVERLAP) if c.strip()])
    log(f"[split] 递归 B({CHUNK_SIZE}/{CHUNK_OVERLAP}): {len(chunks)} 块 / {len(docs)} 篇")

    model = get_model()
    c_emb = encode_cached("chunks_rec_B", chunks, model)
    # 用独立 cache key，别覆盖 D4 那 50 题的题目向量缓存
    q_emb = encode_cached("w6d7_rerank_questions", questions, model)
    vec_scores = q_emb @ c_emb.T

    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)
    bm25 = BM25Okapi([bm25_tokenize(c) for c in chunks])
    top20: list[list[int]] = []
    for i, q in enumerate(questions):
        vt = list(np.argsort(-vec_scores[i])[:TOK_TOPK])
        bt = list(np.argsort(-np.asarray(bm25.get_scores(bm25_tokenize(q))))[:TOK_TOPK])
        fused = rrf(vt, bt, k=RRF_K)
        top20.append([int(idx) for idx, _ in fused[:20]])
    return chunks, top20


def check_baseline(chunks, top20, rows, by_id) -> int:
    """确认重建的 RRF top-5 与 D4 现成检索上下文逐字一致，保证只变排序。"""
    ok = 0
    for row, cand in zip(rows, top20, strict=True):
        d4 = by_id.get(row["id"], {}).get("retrieved_contexts") or []
        mine = [chunks[i] for i in cand[:TOP_K]]
        if len(d4) == TOP_K and all(a.strip() == b.strip()
                                    for a, b in zip(d4, mine, strict=True)):
            ok += 1
    return ok


def rerank_top5(questions, chunks, top20, candidate_n: int):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(4)
    tok = AutoTokenizer.from_pretrained(str(RESOURCE))
    ce = AutoModelForSequenceClassification.from_pretrained(str(RESOURCE))
    ce.eval()
    log(f"[reranker] {RESOURCE.name} / {sum(p.numel() for p in ce.parameters()) / 1e6:.0f}M 参数")

    lists: list[list[int]] = []
    lat: list[float] = []
    for i, (q, cand) in enumerate(zip(questions, top20, strict=True), 1):
        idxs = cand[:candidate_n]
        pairs = [(q, chunks[j]) for j in idxs]
        t0 = time.perf_counter()
        batch = tok(pairs, padding=True, truncation=True,
                    max_length=512, return_tensors="pt")
        with torch.no_grad():
            scores = ce(**batch).logits.view(-1).float().numpy()
        lat.append((time.perf_counter() - t0) * 1000)
        order = np.argsort(-scores)
        lists.append([idxs[int(j)] for j in order[:TOP_K]])
        if i % 5 == 0:
            log(f"[rerank] {i}/{len(questions)}")
    return lists, lat


def atom_coverage(row: dict, ctx_texts: list[str]) -> float | None:
    atoms = row.get("reference_atoms") or []
    if not atoms:
        return None
    norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    blob = norm("\n".join(ctx_texts))
    hit = sum(1 for a in atoms if norm(str(a)) in blob)
    return round(hit / len(atoms), 6)


def score_cp(rows, by_id, base_ctx, rerank_ctx, judge: str, batch_size: int) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate

    env = {
        "RAGAS_JUDGE_BASE_URL": os.environ.get("RAGAS_JUDGE_BASE_URL", ""),
        "LLM_PRIMARY_API_KEY": os.environ.get("LLM_PRIMARY_API_KEY", ""),
    }
    if not (env["RAGAS_JUDGE_BASE_URL"] and env["LLM_PRIMARY_API_KEY"]):
        raise SystemExit("缺 judge 通道环境变量：RAGAS_JUDGE_BASE_URL / LLM_PRIMARY_API_KEY")
    llm, emb, metrics, run_config = build_evaluator(env, 2, 3, 60, judge)
    cp = next(m for m in metrics if m.name == CP_KEY)

    items: list[tuple[str, SingleTurnSample]] = []
    for i, row in enumerate(rows):
        resp = by_id[row["id"]]["response"]
        for group, ctx in (("rrf", base_ctx[i]), ("rerank", rerank_ctx[i])):
            items.append((f"{row['id']}:{group}", SingleTurnSample(
                user_input=row["question"], response=resp,
                retrieved_contexts=[str(c) for c in ctx],
                reference=str(row["reference"]),
            )))
    log(f"[cp] 裁判 {judge} | {len(items)} 个样本 | 批次 {batch_size}")
    out: dict[str, float | None] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        dataset = EvaluationDataset(samples=[s for _, s in batch])
        t0 = time.time()
        try:
            result = evaluate(dataset, metrics=[cp], llm=llm, embeddings=emb,
                              run_config=run_config, raise_exceptions=False,
                              show_progress=False)
            records = to_records(result)
        except Exception as exc:  # noqa: BLE001
            log(f"[cp-fail] {type(exc).__name__}: {str(exc)[:120]}")
            records = []
        for j, (key, _sample) in enumerate(batch):
            out[key] = num(records[j].get(CP_KEY)) if j < len(records) else None
        log(f"[cp] {min(start + batch_size, len(items))}/{len(items)} | {time.time() - t0:.0f}s")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default="glm-5.3-flash")
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--candidates", type=int, default=10, help="精排候选数，重排后取 top-5")
    args = ap.parse_args()

    if not (RESOURCE / "model.safetensors").exists():
        raise SystemExit("缺 models/bge-reranker-base，先跑 scripts/fetch_reranker_model.sh")

    rows = pick_rows()
    log(f"[data] 分层抽样 {len(rows)} 题 | {SAMPLE}")
    questions = [r["question"] for r in rows]
    by_id = {r["id"]: r for r in
             (json.loads(line) for line in RESPONSES.read_text(encoding="utf-8").splitlines()
              if line.strip())}

    chunks, top20 = build_candidates(questions)
    same = check_baseline(chunks, top20, rows, by_id)
    log(f"[baseline] 重建 RRF top-5 与 D4 一致 {same}/{len(rows)}")
    if same != len(rows):
        log("[abort] 基线不一致，继续跑会多出一个变量；先查切法/缓存")
        return 2

    rerank_lists, rerank_latency_ms = rerank_top5(questions, chunks, top20, args.candidates)
    base_ctx = [[chunks[i] for i in c[:TOP_K]] for c in top20]
    rerank_ctx = [[chunks[i] for i in c] for c in rerank_lists]

    cp = score_cp(rows, by_id, base_ctx, rerank_ctx, args.judge, args.batch_size)

    groups: dict[str, dict] = {}
    per_question = []
    for row, bctx, rctx in zip(rows, base_ctx, rerank_ctx, strict=True):
        rid = row["id"]
        pair = {
            "id": rid, "type": row["type"],
            "cp_rrf": cp.get(f"{rid}:rrf"), "cp_rerank": cp.get(f"{rid}:rerank"),
            "atoms_rrf": atom_coverage(row, bctx),
            "atoms_rerank": atom_coverage(row, rctx),
        }
        pair["cp_delta"] = (round(pair["cp_rerank"] - pair["cp_rrf"], 6)
                            if pair["cp_rrf"] is not None and pair["cp_rerank"] is not None
                            else None)
        per_question.append(pair)
        g = groups.setdefault(row["type"], {"n": 0, "cp_rrf": [], "cp_rerank": [],
                                            "atoms_rrf": [], "atoms_rerank": []})
        g["n"] += 1
        for key in ("cp_rrf", "cp_rerank", "atoms_rrf", "atoms_rerank"):
            if pair[key] is not None:
                g[key].append(pair[key])

    def summary(vals):
        return {"mean": round(statistics.fmean(vals), 6), "valid": len(vals)} if vals else None

    all_cp_rrf = [p["cp_rrf"] for p in per_question if p["cp_rrf"] is not None]
    all_cp_rk = [p["cp_rerank"] for p in per_question if p["cp_rerank"] is not None]
    paired = [(p["cp_rrf"], p["cp_rerank"]) for p in per_question
              if p["cp_rrf"] is not None and p["cp_rerank"] is not None]
    paired_delta = [round(b - a, 6) for a, b in paired]

    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "judge": args.judge,
        "sample": SAMPLE,
        "candidates": args.candidates,
        "candidate_pool": "离线 RRF（向量 top-10 + BM25 top-10，k=60）",
        "baseline_verified_against_d4": f"{same}/{len(rows)}",
        "cp_rrf": summary(all_cp_rrf),
        "cp_rerank": summary(all_cp_rk),
        "cp_paired_delta": summary(paired_delta) if paired_delta else None,
        "cp_delta_stdev": (round(statistics.pstdev(paired_delta), 6)
                           if len(paired_delta) > 1 else None),
        "cp_up_down_flat": {
            "up": sum(1 for d in paired_delta if d > 0),
            "down": sum(1 for d in paired_delta if d < 0),
            "flat": sum(1 for d in paired_delta if d == 0),
        },
        "atoms_rrf": summary([p["atoms_rrf"] for p in per_question
                              if p["atoms_rrf"] is not None]),
        "atoms_rerank": summary([p["atoms_rerank"] for p in per_question
                                 if p["atoms_rerank"] is not None]),
        "rerank_latency_ms": {
            "p50": round(statistics.median(rerank_latency_ms), 1),
            "p95": round(sorted(rerank_latency_ms)[int(len(rerank_latency_ms) * 0.95) - 1], 1),
            "n": len(rerank_latency_ms),
        },
        "by_type": {
            k: {key: summary(v[key]) if isinstance(v[key], list) and v[key]
                and isinstance(v[key][0], (int, float)) else v[key]
                for key in ("n", "cp_rrf", "cp_rerank", "atoms_rrf", "atoms_rerank")}
            for k, v in groups.items()
        },
        "per_question": per_question,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=== RRF top-5 → rerank 后 top-5 · context precision ===")
    log(f"  RRF    {report['cp_rrf']}")
    log(f"  rerank {report['cp_rerank']}")
    log(f"  配对 Δ {report['cp_paired_delta']} | σ {report['cp_delta_stdev']} "
        f"| {report['cp_up_down_flat']}")
    log(f"  atoms 覆盖率 RRF {report['atoms_rrf']} → rerank {report['atoms_rerank']}")
    log(f"  精排延迟 P50 {report['rerank_latency_ms']['p50']} ms "
        f"/ P95 {report['rerank_latency_ms']['p95']} ms")
    log(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
