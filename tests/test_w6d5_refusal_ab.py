"""拒答 A/B 扩展实验的纯函数测试：不联网、不读凭据、不动真实产物。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "refusal_ab", ROOT / "scripts" / "w6d5_refusal_ab.py"
)
ra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ra)


def _row(qid: str, typ: str, must: bool, text: str) -> dict:
    return {
        "id": qid,
        "type": typ,
        "must_refuse": must,
        "response": text,
        "model": "deepseek-v4-flash",
    }


def test_is_refusal():
    assert ra.is_refusal("资料中没有覆盖这个问题，无法回答。")
    assert ra.is_refusal("这个问题无法回答")
    assert not ra.is_refusal("当前用官方源即可。")


def test_behavior_groups_and_flips():
    groups = {
        "A": [
            _row("g1", "refuse", True, "资料中没有覆盖这个问题，无法回答。"),
            _row("g2", "refuse", True, "编造了一段答案"),
            _row("g3", "boundary", False, "资料中没有覆盖这个问题，无法回答。"),
        ],
        "B": [
            _row("g1", "refuse", True, "资料中没有覆盖这个问题，无法回答。"),
            _row("g2", "refuse", True, "编造了一段答案"),
            _row("g3", "boundary", False, "这次给出了答案"),
        ],
    }
    b = ra.behavior(groups)
    assert b["refuse"]["n"] == 2
    assert b["refuse"]["A_refused"] == 1 and b["refuse"]["B_refused"] == 1
    assert b["refuse"]["flips"] == []
    assert b["refuse"]["not_refused_A"] == ["g2"]
    assert b["boundary"]["n"] == 1
    assert b["boundary"]["A_refused"] == 1 and b["boundary"]["B_refused"] == 0
    assert b["boundary"]["flips"] == ["g3"]


def test_summary_uses_paired_rows_only():
    groups = {
        "A": [_row("g1", "refuse", True, "x"), _row("g2", "refuse", True, "y")],
        "B": [_row("g1", "refuse", True, "x"), _row("g2", "refuse", True, "y")],
    }
    scores = {
        ("A", "g1"): {"answer_relevancy": 0.5},
        ("B", "g1"): {"answer_relevancy": 0.7},
        ("A", "g2"): {"answer_relevancy": 0.9},
        ("B", "g2"): {},  # 一侧缺分，不能混进均值
    }
    s = ra.build_summary(
        groups, scores, ra.behavior(groups), SimpleNamespace(judge="glm-5.3-flash")
    )
    m = s["by_group"]["refuse"]["metrics"]["answer_relevancy"]
    assert m["n_paired"] == 1
    assert m["mean_A"] == 0.5 and m["mean_B"] == 0.7 and m["delta"] == 0.2
