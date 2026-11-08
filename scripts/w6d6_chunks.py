# -*- coding: utf-8 -*-
"""W6 D6 非 LLM 门禁的公共底座：冻结语料切块 + 纯标准库检索。

为什么不直接 import w6d1_chunk_bench：那个脚本要 numpy / torch / sentence-transformers，
CI 里装不动也不该装。这里只复刻它的递归 B(400/50) 切分，再配一份零依赖 BM25，
用「330 块 / 104,798 字符」跟 D1 对账，保证 source_chunk 的块号口径一致。

只用 Python 标准库，GitHub Actions 的 setup-python 直接能跑。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "corpus"
MANIFEST = CORPUS / "manifest.json"

CHUNK_SIZE, CHUNK_OVERLAP = 400, 50
EXPECTED_CHUNKS = 330
EXPECTED_CHARS = 104798

SEPS = ["\n## ", "\n### ", "\n\n", "。", "；", "，", "\n", " "]


def norm(text: str) -> str:
    """压掉连续空白，让跨行的原文摘引也能逐字核回块文本。"""
    return re.sub(r"\s+", " ", text or "").strip()


def load_corpus() -> list[tuple[str, str]]:
    """按 manifest 顺序读六篇冻结快照，并核对 sha256 与体积。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for entry in manifest["files"]:
        path = CORPUS / entry["file"]
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"冻结语料被改动，sha256 不符: {entry['file']}")
        if len(raw) != entry["bytes"]:
            raise ValueError(f"冻结语料体积变化: {entry['file']}")
        out.append((entry["file"], raw.decode("utf-8")))
    return out


# ---- 递归 B 切分：与 scripts/w6d1_chunk_bench.py 的 split_recursive 逐行对齐 ----

def _split_keep(text: str, sep: str) -> list[str]:
    parts = text.split(sep)
    if len(parts) == 1:
        return [text]
    out = [p + sep for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _break_to_size(text: str, seps: list[str], size: int) -> list[str]:
    if len(text) <= size:
        return [text] if text else []
    if not seps:
        return [text[i : i + size] for i in range(0, len(text), size)]
    out: list[str] = []
    for part in _split_keep(text, seps[0]):
        if len(part) <= size:
            out.append(part)
        else:
            out.extend(_break_to_size(part, seps[1:], size))
    return [x for x in out if x]


def _pack(pieces: list[str], size: int) -> list[str]:
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


def split_recursive(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    base = _pack(_break_to_size(text, SEPS, size), size)
    if overlap <= 0 or len(base) < 2:
        return base
    out = [base[0]]
    for prev, cur in zip(base, base[1:]):
        out.append(prev[-overlap:] + cur)
    return out


def build_chunks() -> list[dict]:
    """逐篇切块、块不跨文档；返回带全局块号的 330 条记录。"""
    docs = load_corpus()
    chunks: list[dict] = []
    total_chars = 0
    for name, text in docs:
        total_chars += len(text)
        parts = [c for c in split_recursive(text) if c.strip()]
        for local_idx, part in enumerate(parts):
            chunks.append({"file": name, "local_idx": local_idx, "text": part})
    for global_idx, chunk in enumerate(chunks):
        chunk["chunk_id"] = global_idx
        chunk["source_chunk"] = f"{chunk['file']}#{global_idx}"
    if total_chars != EXPECTED_CHARS or len(chunks) != EXPECTED_CHUNKS:
        raise ValueError(
            f"切块口径与 D1 不一致: 字符 {total_chars} (期望 {EXPECTED_CHARS})，"
            f"块数 {len(chunks)} (期望 {EXPECTED_CHUNKS})"
        )
    return chunks


# ---- 零依赖检索：中文字符 unigram + bigram 的 BM25 ----

def tokenize(text: str) -> list[str]:
    """不装 jieba，用字符 unigram + bigram 近似中文分词。

    门禁要的是「确定性 + 能拦回归」，不是复现 D2 的 jieba 口径；两者不混用数字。
    """
    compact = re.sub(r"\s+", "", text or "")
    units: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", compact):
        token = match.group()
        units.append(token.lower() if re.match(r"[A-Za-z0-9_]", token) else token)
    return units + [a + b for a, b in zip(units, units[1:])]


class BM25Index:
    """预先算好 df / tf / avgdl，20 题抽样子集跑一次只要几秒。"""

    def __init__(self, chunks: list[dict], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tokens = [tokenize(c["text"]) for c in chunks]
        self.tf = [Counter(t) for t in self.tokens]
        self.df: Counter = Counter()
        for tokens in self.tokens:
            for token in set(tokens):
                self.df[token] += 1
        self.n = len(chunks)
        self.avgdl = sum(len(t) for t in self.tokens) / self.n

    def topk(self, query: str, k: int = 5) -> list[int]:
        q = set(tokenize(query))
        scores: list[tuple[float, int]] = []
        for idx in range(self.n):
            tf, dl = self.tf[idx], len(self.tokens[idx])
            score = 0.0
            for token in q:
                freq = tf.get(token, 0)
                if not freq:
                    continue
                idf = math.log(1 + (self.n - self.df[token] + 0.5) / (self.df[token] + 0.5))
                score += idf * (freq * (self.k1 + 1)) / (
                    freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            scores.append((score, idx))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [idx for _, idx in scores[:k]]


def load_golden(path: Path | None = None) -> list[dict]:
    target = path or ROOT / "evals" / "golden_set.jsonl"
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    blocks = build_chunks()
    index = BM25Index(blocks)
    print(f"chunks={len(blocks)} docs={len({c['file'] for c in blocks})} "
          f"chars={sum(len(c['text']) for c in blocks)}")
    print("sample top-5:", index.topk("HPA 的 CPU 目标值为什么只有在 requests 贴近真实用量时才有意义"))
