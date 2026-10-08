#!/usr/bin/env python
"""pgvector 读写自检 —— 验证「向量写入 + 相似度检索」真的通了。

八周计划 W1 D2 产出物。D2 的验收原话是「compose 起 Postgres + pgvector，
跑通一次向量写入与查询」，本脚本就是那一次。

⚠️ 这个脚本不需要 embeddings 模型 / API Key：
   向量用手造的确定性向量（便于断言结果），目的是验证**数据库侧**通路。

用法：
    python infra/check_pgvector.py
    python infra/check_pgvector.py --dsn "$PGVECTOR_DSN"

    # 或显式给连接串（把尖括号里的替换成自己的值）
    python infra/check_pgvector.py --dsn 'postgresql://<user>:<password>@<host>:5432/<db>'

（默认从 .env 的 POSTGRES_* 变量拼装连接串，通常不用传 --dsn）

依赖：psycopg[binary]>=3.2  （不在 requirements.txt 里，属于开发/验证工具）
    pip install "psycopg[binary]>=3.2"

退出码：0 = 全部通过；1 = 有断言失败；2 = 连不上数据库。
"""

from __future__ import annotations

import argparse
import os
import sys

# ---- 手造的确定性向量 -------------------------------------------------------
# 3 维便于口算验证；真实场景是 1536（text-embedding-3-small）或 1024。
# 设计成「北极 / 赤道 / 南极」三簇，让最近邻结果有明确预期。
ROWS: list[tuple[str, list[float]]] = [
    ("条款A · 资金审批权限", [1.0, 0.0, 0.0]),   # 北极
    ("条款B · 银行承兑汇票", [0.98, 0.19, 0.0]),  # 近北极（与 A 余弦相似度 ~0.98）
    ("条款C · 报销单据要求", [0.0, 1.0, 0.0]),   # 赤道
    ("条款D · 固定资产折旧", [0.0, 0.0, 1.0]),   # 南极
]

QUERY = [1.0, 0.05, 0.0]  # 近乎北极 → 期望招回 A、B（都应是高相似度）
EXPECT_TOP = {"条款A · 资金审批权限", "条款B · 银行承兑汇票"}


def main() -> int:
    ap = argparse.ArgumentParser(description="pgvector 读写自检")
    ap.add_argument(
        "--dsn",
        default=os.getenv(
            "PGVECTOR_DSN",
            "postgresql://{u}:{p}@{h}:{port}/{db}".format(
                u=os.getenv("POSTGRES_USER", "llmops"),
                p=os.getenv("POSTGRES_PASSWORD", ""),
                h=os.getenv("POSTGRES_HOST", "localhost"),
                port=os.getenv("POSTGRES_PORT", "5432"),
                db=os.getenv("POSTGRES_DB", "llmops"),
            ),
        ),
        help="连接串，默认从 POSTGRES_* 环境变量拼装",
    )
    args = ap.parse_args()

    try:
        import psycopg
    except ImportError:
        print("[FAIL] 缺少依赖 psycopg，请先： pip install \"psycopg[binary]>=3.2\"")
        return 2

    print(f"[连接] {args.dsn.replace(':' + os.getenv('POSTGRES_PASSWORD', 'x'), ':***')}")
    try:
        conn = psycopg.connect(args.dsn, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 连不上数据库：{e}")
        print("       检查：docker compose -f infra/compose.yml ps 是否 healthy；.env 里 POSTGRES_PASSWORD 是否已填")
        return 2

    ok = True
    with conn:
        with conn.cursor() as cur:
            # ---- 0. 扩展在位吗 ----
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector';")
            if not cur.fetchone():
                print("[FAIL] vector 扩展未启用 —— 数据卷可能是旧初始化的，跑 down -v 重来")
                return 1
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
            print(f"[OK] pgvector 已启用，版本 {cur.fetchone()[0]}")

            # ---- 1. 建表（含向量列）----
            # 维度 3 与 ROWS 对齐。真实项目这里会是 vector(1536)。
            cur.execute("DROP TABLE IF EXISTS demo_vectors;")
            cur.execute(
                """
                CREATE TABLE demo_vectors (
                    id    SERIAL PRIMARY KEY,
                    label TEXT NOT NULL,
                    embedding vector(3) NOT NULL
                );
                """
            )
            print("[OK] 建表 demo_vectors(label, embedding vector(3))")

            # ---- 2. 写入 ----
            cur.executemany(
                "INSERT INTO demo_vectors (label, embedding) VALUES (%s, %s);",
                [(label, str(vec)) for label, vec in ROWS],
            )
            print(f"[OK] 写入 {len(ROWS)} 条向量")

            # ---- 3. 相似度检索：<=> 是余弦距离（越小越相似）----
            cur.execute(
                """
                SELECT label, 1 - (embedding <=> %s::vector) AS cosine_similarity
                  FROM demo_vectors
                 ORDER BY embedding <=> %s::vector
                 LIMIT 2;
                """,
                (str(QUERY), str(QUERY)),
            )
            hits = cur.fetchall()

    print("\n[检索] 查询向量接近北极，Top-2 结果：")
    for label, sim in hits:
        print(f"   {sim:.4f}  {label}")

    got = {h[0] for h in hits}
    if got != EXPECT_TOP:
        print(f"\n[FAIL] 期望召回 {EXPECT_TOP}，实际 {got}")
        ok = False
    else:
        print(f"\n[OK] 召回符合预期 —— 向量写入与相似度检索通路已打通")

    # ---- 4. 顺手证明「索引」这个概念（真实规模才需要，此处仅演示语法）----
    if ok:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS demo_vectors_ivfflat "
                    "ON demo_vectors USING ivfflat (embedding vector_cosine_ops) "
                    "WITH (lists = 10);"
                )
                print("[OK] 已建 ivfflat 余弦索引（小数据用不上，仅验证语法可用）")

    conn.close()
    print("\n" + ("=" * 52))
    print("结论：pgvector 读写自检 " + ("全部通过 ✅" if ok else "存在失败 ❌"))
    print("=" * 52)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
