"""应用侧 pgvector 连接入口（DEC-041）。

W6 D1 实测：IVFFlat 默认 ``ivfflat.probes = 1`` 会把精确检索的 hit@5 10/10
掉成 9/10、9/10、7/10，漏题还会随索引重建漂移；显式设 3 之后三次稳定 10/10。
此前这条 SET 只散在 ``scripts/w6d1_verify_pgvector.py`` 与
``scripts/w6d2_rerank_bench.py`` 里，应用侧没有统一入口。本模块把
「连接即设 probes」收敛到一处，避免以后接 RAG 服务时忘了设。

⚠️ 2026-11-08 现状：``app/`` 目前只有 ``/chat``、指标与路由，**没有 RAG 检索端点**，
本模块暂无生产调用方。等 W7 接检索服务时统一走 ``connect_pgvector()``，
不要再在业务代码里手写 SET。
"""
from __future__ import annotations

import os

# Windows 上 localhost 会先解析 ::1，网关/pg 只听 IPv4 时每次新建连接多等约 2s。
PG_HOST = "127.0.0.1"
DEFAULT_IVFFLAT_PROBES = 3
PROBES_ENV = "IVFFLAT_PROBES"
MIN_PROBES, MAX_PROBES = 1, 100


def ivfflat_probes() -> int:
    """会话默认 probes：环境变量可覆盖，非法值回退到 3。"""
    raw = os.getenv(PROBES_ENV, str(DEFAULT_IVFFLAT_PROBES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IVFFLAT_PROBES
    return value if MIN_PROBES <= value <= MAX_PROBES else DEFAULT_IVFFLAT_PROBES


def probes_sql(probes: int | None = None) -> str:
    """生成会话级 SET。SET 不支持绑定参数，所以先校验整数再拼字面量。"""
    value = ivfflat_probes() if probes is None else probes
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("ivfflat.probes 必须是整数")
    if not MIN_PROBES <= value <= MAX_PROBES:
        raise ValueError(f"ivfflat.probes 必须在 {MIN_PROBES}..{MAX_PROBES} 之间")
    return f"SET ivfflat.probes = {value}"


def connect_pgvector(env: dict[str, str] | None = None, probes: int | None = None):
    """建立 psycopg 连接，并立刻把 ivfflat.probes 设成本会话默认值。

    psycopg 只在 requirements-eval.txt 里，app 运行时依赖没有它，所以延迟导入；
    单测用 sys.modules 注入假驱动，不需要真库。
    """
    import psycopg

    cfg = os.environ if env is None else env
    conn = psycopg.connect(
        host=PG_HOST,
        port=int(cfg["POSTGRES_PORT"]),
        dbname=cfg["POSTGRES_DB"],
        user=cfg["POSTGRES_USER"],
        password=cfg["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )
    with conn.cursor() as cur:
        cur.execute(probes_sql(probes))
    return conn
