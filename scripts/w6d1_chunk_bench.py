# -*- coding: utf-8 -*-
"""W6 D1 动手实践：三种切法 x 5 行配置的实跑基准。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d1_chunk_bench.py

产出:
    evals/out/w6d1/results.json        完整结果（UTF-8）
    evals/out/w6d1/cache/*.npy         embedding 缓存；重跑自动复用，可中断续跑

口径:
    - 语料 = evals/corpus/ 六篇**冻结快照**（取自 commit 6ba1baf，104,798 字符），
      逐篇切分，块不跨文档。冻结原因：docs/decisions.md 会随决策日志增长，
      直接读 docs/ 会让同一份实验每次跑出不同数字（2026-11-05 实测污染 +5,299 字符）。
      版本清单见 evals/corpus/manifest.json。
    - 递归切：按分隔符优先级切到 <= chunk_size，贪心合并；
      从第二块起把前一块末尾 overlap 个字符复制为前缀（块长上限 = size + overlap）。
    - 语义切：按句末标点切句，bge-small-zh-v1.5 算相邻句余弦，
      低于该文档 95 百分位处断开（LlamaIndex SemanticSplitterNodeParser 思路）。
    - 检索：同一模型编码问题与块，余弦取 top-5；
      命中 = top-5 中至少一块包含该题 expect_keywords 里的关键词。
    - 冗余率 = (块总字符 - 原文字符) / 块总字符，衡量 overlap 带来的重复文本。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
# 语料读冻结快照，不读 docs/：决策日志会持续增长，直接读 docs/ 会让基线不可复现。
DOCS_DIR = EVALS_DIR / "corpus"
OUT_DIR = EVALS_DIR / "out" / "w6d1"
CACHE_DIR = OUT_DIR / "cache"
MODEL_DIR = ROOT / "models" / "bge-small-zh-v1.5"
PROBE_FILE = EVALS_DIR / "chunk_probe_set.jsonl"

OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SEPS = ["\n## ", "\n### ", "\n\n", "。", "；", "，", "\n", " "]
TAIL_OK = set("。！？；\n.!?;:)]}>」》")
SENT_SPLIT = re.compile(r"(?<=[。！？\n])")


def log(msg: str) -> None:
    print(msg, flush=True)


def load_docs():
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(DOCS_DIR.glob("*.md"))]


def load_probes():
    return [json.loads(line) for line in PROBE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def get_model():
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(4)
    log("[model] loading bge-small-zh-v1.5 from local dir ...")
    return SentenceTransformer(str(MODEL_DIR), device="cpu")


def encode_cached(key: str, texts: list[str], model, batch_size: int = 64):
    """按内容签名缓存 embedding。中途被打断后重跑会跳过已算完的部分。"""
    emb_file = CACHE_DIR / f"{key}.npy"
    meta_file = CACHE_DIR / f"{key}.json"
    sig = hashlib.md5("\x00".join(texts).encode("utf-8")).hexdigest()
    if emb_file.exists() and meta_file.exists():
        if json.loads(meta_file.read_text(encoding="utf-8"))["sig"] == sig:
            log(f"[encode] {key}: cache hit ({len(texts)} texts)")
            return np.load(emb_file)
    t0 = time.time()
    log(f"[encode] {key}: encoding {len(texts)} texts ...")
    emb = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    emb = np.asarray(emb, dtype=np.float32)
    np.save(emb_file, emb)
    meta_file.write_text(
        json.dumps({"sig": sig, "n": len(texts), "sec": round(time.time() - t0, 1)}),
        encoding="utf-8",
    )
    log(f"[encode] {key}: done in {time.time() - t0:.1f}s, dim={emb.shape[1]}")
    return emb


# ---------------- 切法 ----------------

def split_fixed(text: str, size: int = 512):
    return [text[i : i + size] for i in range(0, len(text), size)]


def _split_keep(text: str, sep: str):
    parts = text.split(sep)
    if len(parts) == 1:
        return [text]
    out = [p + sep for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _break_to_size(text: str, seps: list[str], size: int):
    if len(text) <= size:
        return [text] if text else []
    if not seps:
        return [text[i : i + size] for i in range(0, len(text), size)]
    out = []
    for part in _split_keep(text, seps[0]):
        if len(part) <= size:
            out.append(part)
        else:
            out.extend(_break_to_size(part, seps[1:], size))
    return [x for x in out if x]


def _pack(pieces: list[str], size: int):
    chunks, buf = [], ""
    for p in pieces:
        if len(buf) + len(p) <= size:
            buf += p
        else:
            if buf:
                chunks.append(buf)
            buf = p
            while len(buf) > size:
                chunks.append(buf[:size])
                buf = buf[size:]
    if buf:
        chunks.append(buf)
    return chunks


def split_recursive(text: str, size: int, overlap: int):
    base = _pack(_break_to_size(text, SEPS, size), size)
    if overlap <= 0 or len(base) < 2:
        return base
    out = [base[0]]
    for prev, cur in zip(base, base[1:]):
        out.append(prev[-overlap:] + cur)
    return out


def build_semantic_chunks(docs, model):
    per_doc, all_sents = [], []
    for _, text in docs:
        sents = [s for s in SENT_SPLIT.split(text) if s.strip()]
        per_doc.append(sents)
        all_sents.extend(sents)
    emb = encode_cached("semantic_sentences", all_sents, model)
    chunks, off, thresholds = [], 0, []
    for sents in per_doc:
        e = emb[off : off + len(sents)]
        off += len(sents)
        if len(sents) < 3:
            chunks.extend(sents)
            continue
        sims = np.sum(e[:-1] * e[1:], axis=1)
        # LlamaIndex SemanticSplitterNodeParser 口径：用「距离 = 1 - 余弦」的 p95，
        # 距离高于阈值（即最不相似的约 5%）才断开。若误用相似度 p95 当断点，
        # 95% 的句子都会被切开，语义切会退化成逐句切。
        dists = 1.0 - sims
        thr = float(np.percentile(dists, 95))
        thresholds.append(round(thr, 4))
        cur = sents[0]
        for i in range(1, len(sents)):
            if dists[i - 1] > thr:
                chunks.append(cur)
                cur = sents[i]
            else:
                cur += sents[i]
        chunks.append(cur)
    log(f"[semantic] sentences={len(all_sents)} per-doc distance-p95 thresholds={thresholds}")
    return [c for c in chunks if c.strip()]


def per_docs(docs, fn):
    out = []
    for _, text in docs:
        out.extend(fn(text))
    return [c for c in out if c.strip()]


# ---------------- 指标 ----------------

def tail_complete(chunks):
    if not chunks:
        return 0.0
    ok = sum(1 for c in chunks if c.rstrip() and c.rstrip()[-1] in TAIL_OK)
    return round(100.0 * ok / len(chunks), 1)


def evaluate(key, label, params, chunks, q_emb, probes, model, corpus_chars):
    c_emb = encode_cached(f"chunks_{key}", chunks, model)
    scores = q_emb @ c_emb.T
    detail = []
    for i, p in enumerate(probes):
        top5 = np.argsort(-scores[i])[:5]
        kws = p["expect_keywords"]
        hit = any(all(k in chunks[j] for k in kws) for j in top5)
        hit1 = all(k in chunks[int(top5[0])] for k in kws)
        detail.append(
            {
                "id": p["id"],
                "keywords": kws,
                "hit": bool(hit),
                "hit_at1": bool(hit1),
                "top1_score": round(float(scores[i][top5[0]]), 4),
            }
        )
    total_chars = sum(len(c) for c in chunks)
    redundancy = max(0.0, (total_chars - corpus_chars) / total_chars * 100) if total_chars else 0.0
    row = {
        "key": key,
        "label": label,
        "params": params,
        "chunks": len(chunks),
        "avg_len": round(total_chars / len(chunks), 1) if chunks else 0,
        "redundancy_pct": round(redundancy, 2),
        "hit_at5": sum(1 for d in detail if d["hit"]),
        "hit_at1": sum(1 for d in detail if d["hit_at1"]),
        "probes": len(probes),
        "tail_complete_pct": tail_complete(chunks),
        "detail": detail,
    }
    log(
        "[result] {:<12} chunks={:<5} avg_len={:<7} redundancy={:<6}% hit@5={}/{} hit@1={}/{} tail_ok={}%".format(
            key, row["chunks"], row["avg_len"], row["redundancy_pct"],
            row["hit_at5"], row["probes"], row["hit_at1"], row["probes"],
            row["tail_complete_pct"],
        )
    )
    return row


def main():
    docs = load_docs()
    probes = load_probes()
    corpus_chars = sum(len(t) for _, t in docs)
    log(f"[corpus] {len(docs)} docs, {corpus_chars} chars, {len(probes)} probes")
    model = get_model()
    q_emb = encode_cached("probes", [p["question"] for p in probes], model)

    specs = OrderedDict()
    specs["fixed_512"] = ("固定长度", "512 字符 / 无 overlap", lambda: per_docs(docs, lambda t: split_fixed(t, 512)))
    specs["rec_A"] = ("递归 A", "200 / 25（步长 175）", lambda: per_docs(docs, lambda t: split_recursive(t, 200, 25)))
    specs["rec_B"] = ("递归 B", "400 / 50（步长 350）", lambda: per_docs(docs, lambda t: split_recursive(t, 400, 50)))
    specs["rec_C"] = ("递归 C", "400 / 100（步长 300）", lambda: per_docs(docs, lambda t: split_recursive(t, 400, 100)))
    specs["semantic"] = ("语义切", "breakpoint 阈值 p95", lambda: build_semantic_chunks(docs, model))

    rows = []
    for key, (label, params, builder) in specs.items():
        t0 = time.time()
        chunks = builder()
        log(f"[split] {key}: {len(chunks)} chunks in {time.time() - t0:.1f}s")
        rows.append(evaluate(key, label, params, chunks, q_emb, probes, model, corpus_chars))

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "corpus": {"dir": "evals/corpus/*.md (frozen @ 6ba1baf)", "files": [n for n, _ in docs], "chars": corpus_chars},
        "model": "models/bge-small-zh-v1.5 (bge-small-zh-v1.5, 512-dim, cosine)",
        "probe_set": "evals/chunk_probe_set.jsonl",
        "metric_notes": {
            "hit_at5": "top-5 cosine, hit if any chunk contains all expect_keywords",
            "redundancy_pct": "(sum(chunk chars) - corpus chars) / sum(chunk chars) * 100",
            "tail_complete_pct": "share of chunks whose last non-space char is sentence-ending punctuation",
            "recursive_def": "chunk = previous chunk's last `overlap` chars + new content (<= chunk_size)",
            "semantic_def": "LlamaIndex 口径: per-document p95 of (1-cosine) distance; break where distance > threshold",
        },
        "rows": rows,
    }
    out = OUT_DIR / "results.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[done] wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
