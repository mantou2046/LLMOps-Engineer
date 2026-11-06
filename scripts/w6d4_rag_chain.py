# -*- coding: utf-8 -*-
"""W6 D4 动手实践：最小 RAG 链路，为 golden set 的 50 题产出回答与检索上下文。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d4_rag_chain.py --limit 10
    ./.venv-eval/Scripts/python.exe scripts/w6d4_rag_chain.py            # 全量 50 题
    ./.venv-eval/Scripts/python.exe scripts/w6d4_rag_chain.py --force     # 忽略断点重跑

产出（evals/out/ 被 gitignore，产物不进仓库）:
    evals/out/w6d4/responses.jsonl   每题 user_input / response / retrieved_contexts / reference
    evals/out/w6d4/retrieval.json    切块与检索配置、每题 top-5 块编号

口径:
    - 语料 = evals/corpus/ 六篇冻结快照（104,798 字符），切法沿用 W6 D1 选出的递归 B(400/50)。
    - 检索沿用 W6 D2：bge-small-zh-v1.5 余弦 + jieba BM25，RRF(k=60) 融合，取 top-5 块。
      这样 D4 的 context recall 能跟 D1/D2 的 hit@5 / hit@1 对上。
    - 生成走 31415 网关的 deepseek-v4-flash，system prompt 只允许依据给定资料作答，
      资料没覆盖就明确拒答。生成模型与 RAGAS 裁判模型是同一个，局限写进报告。
    - 不碰 app/：这是离线评估链路，不是生产服务。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

OUT_DIR = ROOT / "evals" / "out" / "w6d4"
RESPONSES = OUT_DIR / "responses.jsonl"
RETRIEVAL = OUT_DIR / "retrieval.json"
GOLDEN = ROOT / "evals" / "golden_set.jsonl"

TOP_K = 5
CHUNK_SIZE, CHUNK_OVERLAP = 400, 50
MAX_WORKERS = 2

SYSTEM_PROMPT = """你是一个只依据给定资料回答问题的助手。必须遵守以下规则：
1. 只能用「资料」里的内容作答，不要补充资料之外的数字、命令、结论或常识。
2. 资料里没有能回答这个问题的信息时，只回复一句：资料中没有覆盖这个问题，无法回答。不要猜，不要用通用知识补齐。
3. 回答控制在 2 到 4 句，直接给结论和依据；不要复述问题，不要加「根据资料」这类前缀。
4. 资料里的命令行、表格行、章节标题可以原样引用。
"""

_write_lock = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_golden() -> list[dict]:
    return [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def build_retrieval(questions: list[str]) -> tuple[list[str], list[list[int]], dict]:
    """切块 → 双路检索 → RRF。返回 (块文本, 每题 top-5 块下标, 元信息)。"""
    docs = load_docs()
    chunks: list[str] = []
    chunk_doc: list[str] = []
    for name, text in docs:
        parts = [c for c in split_recursive(text, CHUNK_SIZE, CHUNK_OVERLAP) if c.strip()]
        chunks.extend(parts)
        chunk_doc.extend([name] * len(parts))
    log(f"[split] 递归 B({CHUNK_SIZE}/{CHUNK_OVERLAP}): {len(chunks)} 块 / {len(docs)} 篇")

    model = get_model()
    c_emb = encode_cached("chunks_rec_B", chunks, model)  # 复用 D1 缓存
    q_emb = encode_cached("w6d4_questions", questions, model)
    vec_scores = q_emb @ c_emb.T

    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)
    corpus_tokens = [bm25_tokenize(c) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)

    top_lists: list[list[int]] = []
    for i, q in enumerate(questions):
        vec_top = list(np.argsort(-vec_scores[i])[:TOK_TOPK])
        bm_top = list(np.argsort(-np.asarray(bm25.get_scores(bm25_tokenize(q))))[:TOK_TOPK])
        fused = rrf(vec_top, bm_top, k=RRF_K)
        top_lists.append([int(idx) for idx, _ in fused[:TOP_K]])

    meta = {
        "chunks": len(chunks),
        "docs": [name for name, _ in docs],
        "split": f"recursive_B({CHUNK_SIZE}/{CHUNK_OVERLAP})",
        "retrieval": "bge-small-zh-v1.5 余弦 + jieba BM25，RRF(k=60)，top-5",
        "top_k": TOP_K,
        "chunk_doc": chunk_doc,
        "per_question": [
            {"question": q, "chunk_indices": idxs, "chunk_docs": [chunk_doc[i] for i in idxs]}
            for q, idxs in zip(questions, top_lists)
        ],
    }
    return chunks, top_lists, meta


def call_llm(client, model: str, question: str, contexts: list[str]) -> tuple[str, float]:
    blocks = "\n\n".join(f"[块 {i + 1}]\n{c}" for i, c in enumerate(contexts))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"问题：{question}\n\n资料：\n{blocks}"},
        ],
        "temperature": 0,
        "max_tokens": 400,
    }
    delay = 5.0
    last: Exception | None = None
    for attempt in range(6):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**payload)
            text = (resp.choices[0].message.content or "").strip()
            return text, round(time.time() - t0, 2)
        except Exception as exc:  # 429 / 5xx / 超时，统一退避
            last = exc
            log(f"[retry {attempt + 1}/6] {type(exc).__name__}: {str(exc)[:110]} → sleep {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    return f"[生成失败] {type(last).__name__}: {str(last)[:160]}", 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全量 50）")
    ap.add_argument("--force", action="store_true", help="忽略已有结果，全部重跑")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env()
    rows = load_golden()
    if args.limit:
        rows = rows[: args.limit]

    done: dict[str, dict] = {}
    if RESPONSES.exists() and not args.force:
        kept: list[str] = []
        for line in RESPONSES.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if str(obj.get("response", "")).startswith("[生成失败]"):
                continue  # 失败行不进 done，重跑时自动补上
            kept.append(line)
            done[obj["id"]] = obj
        if len(kept) < len(RESPONSES.read_text(encoding="utf-8").splitlines()):
            RESPONSES.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            log(f"[resume] 清掉失败行，保留 {len(kept)} 题")
    todo = [r for r in rows if r["id"] not in done]
    log(f"[plan] 目标 {len(rows)} 题 | 已完成 {len(rows) - len(todo)} | 本次待跑 {len(todo)}")

    questions = [r["question"] for r in rows]
    chunks, top_lists, meta = build_retrieval(questions)
    RETRIEVAL.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if not todo:
        log("[done] 没有待跑题目，直接复用已有结果")
        return 0

    from openai import OpenAI

    client = OpenAI(
        base_url=env["RAGAS_JUDGE_BASE_URL"],
        api_key=env["LLM_PRIMARY_API_KEY"],
        timeout=180.0,
        max_retries=0,
    )
    model = env["RAGAS_JUDGE_MODEL"]
    index = {r["id"]: i for i, r in enumerate(rows)}
    t_start = time.time()

    def one(row: dict) -> dict:
        i = index[row["id"]]
        ctxs = [chunks[j] for j in top_lists[i]]
        text, latency = call_llm(client, model, row["question"], ctxs)
        return {
            "id": row["id"],
            "user_input": row["question"],
            "response": text,
            "retrieved_contexts": ctxs,
            "reference": row["reference"],
            "must_refuse": bool(row["must_refuse"]),
            "type": row["type"],
            "source_doc": row["source_doc"],
            "model": model,
            "latency_s": latency,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    new: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, r): r["id"] for r in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            rid = futures[fut]
            try:
                item = fut.result()
            except Exception as exc:
                log(f"[fail] {rid}: {type(exc).__name__}: {exc}")
                item = {"id": rid, "response": f"[生成失败] {exc}", "retrieved_contexts": []}
            new.append(item)
            with _write_lock:
                with RESPONSES.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            if n % 5 == 0 or n == len(todo):
                log(f"[gen] {n}/{len(todo)}  done in {time.time() - t_start:.0f}s")

    failed = [x["id"] for x in new if x["response"].startswith("[生成失败]")]
    merged = {**done, **{x["id"]: x for x in new}}
    log(f"[summary] 累计 {len(merged)}/{len(rows)} 题 | 生成失败 {failed or '无'} | 总耗时 {time.time()-t_start:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
