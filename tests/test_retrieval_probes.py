"""DEC-041 应用侧 probes 接线的集成测：假 pg 驱动，不连真实库。"""
from __future__ import annotations

import sys
import types

import pytest
import retrieval


@pytest.fixture
def fake_pg(monkeypatch):
    mod = types.ModuleType("psycopg")
    conn = types.SimpleNamespace(cursor=None)
    cur = types.SimpleNamespace(execute=None)
    executed: list[str] = []
    cur.execute = executed.append

    class CursorCtx:
        def __enter__(self):
            return cur

        def __exit__(self, *args):
            return False

    conn.cursor = CursorCtx
    calls: list[dict] = []

    def connect(**kwargs):
        calls.append(kwargs)
        return conn

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return {"calls": calls, "executed": executed}


ENV = {
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "rag",
    "POSTGRES_USER": "app",
    "POSTGRES_PASSWORD": "dummy",
}


def test_default_probes_sql():
    assert retrieval.probes_sql() == "SET ivfflat.probes = 3"


def test_probes_sql_rejects_bad_values():
    for bad in (0, 101, -1, "3", 3.0, True):
        with pytest.raises(ValueError):
            retrieval.probes_sql(bad)


def test_env_override_and_fallback(monkeypatch):
    monkeypatch.setenv(retrieval.PROBES_ENV, "5")
    assert retrieval.ivfflat_probes() == 5
    monkeypatch.setenv(retrieval.PROBES_ENV, "abc")
    assert retrieval.ivfflat_probes() == 3
    monkeypatch.setenv(retrieval.PROBES_ENV, "999")
    assert retrieval.ivfflat_probes() == 3


def test_connect_sets_probes_and_uses_ipv4(fake_pg, monkeypatch):
    monkeypatch.delenv(retrieval.PROBES_ENV, raising=False)
    conn = retrieval.connect_pgvector(ENV)
    assert conn is not None
    assert fake_pg["calls"][0]["host"] == "127.0.0.1"
    assert fake_pg["calls"][0]["port"] == 5432
    assert fake_pg["executed"] == ["SET ivfflat.probes = 3"]


def test_connect_respects_explicit_probes(fake_pg):
    retrieval.connect_pgvector(ENV, probes=5)
    assert fake_pg["executed"] == ["SET ivfflat.probes = 5"]


def test_connect_respects_env_probes(fake_pg, monkeypatch):
    monkeypatch.setenv(retrieval.PROBES_ENV, "7")
    retrieval.connect_pgvector(ENV)
    assert fake_pg["executed"] == ["SET ivfflat.probes = 7"]
