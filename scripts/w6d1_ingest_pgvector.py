# -*- coding: utf-8 -*-
"""W6 D1 · 把冻结语料灌进 pgvector（11/9「灌库」那一步的可复跑实现）。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d1_ingest_pgvector.py --dry-run   # 只看计划，不动库
    ./.venv-eval/Scripts/python.exe scripts/w6d1_ingest_pgvector.py             # 事务内一次做完

做四件事（全在一个事务里，失败整体回滚）:
    1. 删掉 W4 D2 的 29 行 numpy fake 384 维占位数据（迁表前必须清，维度不兼容）
    2. rag_chunks.embedding: vector(384) -> vector(512)
    3. 用递归 B（400/50）切冻结语料 evals/corpus/*.md，bge-small-zh-v1.5 生成真 512 维向量灌库
    4. 重建 IVFFlat 余弦索引 + ANALYZE

前置:
    - Docker 容器 llmops-postgres 在跑、pgvector 0.8.7 就位
    - .venv-eval 装了 psycopg[binary]、sentence-transformers
    - 迁表前先备份: docker exec llmops-postgres pg_dump -U llmops -d llmops \
        --table=public.rag_chunks > .local-backup/rag_chunks-before-w6-<date>.sql

口径:
    - 语料固定读 evals/corpus/（commit 6ba1baf，104,798 字符），不读 docs/。
      docs/decisions.md 会随决策日志增长，直接读会让灌库行数每次不同。
    - 连接一律用 127.0.0.1；.env 里写的是 localhost，Windows 上会先解析 ::1 卡住。
    - 向量以字符串 '[x,y,...]' 传参并 ::vector 转型，不依赖 pgvector 的 Python 包。
    - 全量覆盖（DELETE + INSERT），不是增量：切法/模型/维度任一变化时按 W4 D2 口径必须 full。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 复用 D1 基准脚本的切法、模型与 embedding 缓存（同一套文本 -> 同一批向量）
from w6d1_chunk_bench import (  # noqa: E402
    encode_cached,
    get_model,
    load_docs,
    split_recursive,
)

CHUNK_SIZE = 400
OVERLAP = 50
EXPECTED_CHUNKS = 330          # D1 手册答案卡：递归 B 在冻结语料上切出的块数
IVFFLAT_LISTS = 10            # 与 W4 D2 一致，便于对照
OUT_FILE = ROOT / "evals" / "out" / "w6d1" / "pgvector_ingest.json"


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def connect(env: dict[str, str]):
    return psycopg.connect(
        host="127.0.0.1",                 # 不要用 env['POSTGRES_HOST']（localhost 会卡 IPv6）
        port=int(env["POSTGRES_PORT"]),
        dbname=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


def to_vector_literal(vec: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不连接数据库、不算向量")
    args = ap.parse_args()

    docs = load_docs()
    corpus_chars = sum(len(t) for _, t in docs)
    manifest = json.loads((ROOT / "evals" / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    if corpus_chars != manifest["total_chars"]:
        raise SystemExit(f"语料与 manifest 不符: {corpus_chars} != {manifest['total_chars']}")

    # 逐篇切分，保留 doc_path -> chunk_idx 映射；块不跨文档
    records = []
    for name, text in docs:
        chunks = [c for c in split_recursive(text, CHUNK_SIZE, OVERLAP) if c.strip()]
        doc_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for idx, content in enumerate(chunks):
            records.append({
                "doc_path": f"evals/corpus/{name}",
                "chunk_idx": idx,
                "doc_sha": doc_sha,
                "chunk_sha": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content,
            })

    print(f"[plan] 语料 {len(docs)} 篇 / {corpus_chars} 字符（冻结 @ {manifest['source_commit']}）")
    print(f"[plan] 递归 B {CHUNK_SIZE}/{OVERLAP} 切出 {len(records)} 块"
          f"（手册基线 {EXPECTED_CHUNKS}）")
    if len(records) != EXPECTED_CHUNKS:
        raise SystemExit("块数与 D1 基线不一致，先查语料/切法，别灌库")
    print("[plan] 迁表 vector(384) -> vector(512)，清掉 W4 D2 的 fake 占位行，重建 IVFFlat")
    if args.dry_run:
        print("[dry-run] 未连接数据库、未写入任何内容")
        return 0

    env = load_env()
    t0 = time.time()
    model = get_model()
    emb = encode_cached(
        "chunks_rec_B",
        [r["content"] for r in records],
        model,
    )
    if emb.shape != (len(records), 512):
        raise SystemExit(f"embedding 维度不对: {emb.shape}")
    encode_sec = round(time.time() - t0, 2)
    print(f"[embed] {emb.shape[0]} 块 / {emb.shape[1]} 维，"
          f"耗时 {encode_sec}s（含模型加载与缓存命中）")

    t_ingest = time.time()
    with connect(env) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), max(vector_dims(embedding)) FROM rag_chunks")
            old_rows, old_dim = cur.fetchone()
            print(f"[before] rag_chunks: {old_rows} 行 / vector({old_dim})")
            if old_rows:
                # 首次跑是 W4 D2 的 29 行 fake 占位；复跑是上一轮灌进去的真数据。
                # 两者都要清空：ALTER 维度必须空表，且全量覆盖是 W4 D2 定的口径。
                print(f"[before] 全量覆盖，清空这 {old_rows} 行")

            cur.execute("DROP INDEX IF EXISTS idx_rag_chunks_emb")
            # TRUNCATE 而不是 DELETE：DELETE 留下的死元组会让 heap/索引在复跑后持续膨胀
            # （实测两轮后表 2272 -> 4360 kB）。PostgreSQL 的 TRUNCATE 可回滚，事务内安全。
            cur.execute("TRUNCATE TABLE rag_chunks")
            cur.execute("ALTER TABLE rag_chunks ALTER COLUMN embedding TYPE vector(512)")

            cur.executemany(
                """
                INSERT INTO rag_chunks
                    (doc_path, chunk_idx, doc_sha, chunk_sha, content, embedding, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (doc_path, chunk_idx) DO UPDATE SET
                    doc_sha = EXCLUDED.doc_sha,
                    chunk_sha = EXCLUDED.chunk_sha,
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    (
                        r["doc_path"], r["chunk_idx"], r["doc_sha"], r["chunk_sha"],
                        r["content"], to_vector_literal(emb[i]), time.time(),
                    )
                    for i, r in enumerate(records)
                ],
            )
            cur.execute(
                "CREATE INDEX idx_rag_chunks_emb ON rag_chunks "
                f"USING ivfflat (embedding vector_cosine_ops) WITH (lists = {IVFFLAT_LISTS})"
            )
            cur.execute("ANALYZE rag_chunks")

    # 体积必须在事务提交后量：事务内查会把刚 DROP 但还没释放的旧索引文件算进去，
    # 实测复跑时虚报成 4360 kB（真实约 2 MB）。
    with connect(env) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), max(vector_dims(embedding)) FROM rag_chunks")
            new_rows, new_dim = cur.fetchone()
            cur.execute("SELECT pg_size_pretty(pg_total_relation_size('rag_chunks'))")
            total_size = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(pg_relation_size('idx_rag_chunks_emb'))")
            idx_size = cur.fetchone()[0]

    ingest_sec = round(time.time() - t_ingest, 2)
    print(f"[after ] rag_chunks: {new_rows} 行 / vector({new_dim}) / "
          f"表 {total_size} / 向量索引 {idx_size}")
    print(f"[timing] 编码 {encode_sec}s，迁表+灌库+建索引 {ingest_sec}s")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "source_commit": manifest["source_commit"],
        "corpus_chars": corpus_chars,
        "chunking": f"recursive B {CHUNK_SIZE}/{OVERLAP}",
        "chunks": len(records),
        "embedding_dim": int(emb.shape[1]),
        "model": "models/bge-small-zh-v1.5",
        "before": {"rows": old_rows, "dim": old_dim},
        "after": {
            "rows": new_rows, "dim": new_dim,
            "table_size": total_size, "index_size": idx_size,
        },
        "ivfflat_lists": IVFFLAT_LISTS,
        "encode_sec": encode_sec,
        "ingest_sec": ingest_sec,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] wrote {OUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
