"""W7 D7 验收核对脚本的判定逻辑单测。

只测「判定」这一层：喂构造出来的 payload，看阈值对不对、边界情况会不会误判 PASS。
不跑 demo、不读真实产物、不联网——真实产物由 `w7d7_acceptance_check.py` 自己重跑。

重点在于：判定函数必须能对**坏数据说 FAIL**，否则 PASS 没有意义。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "w7d7_acceptance_check", ROOT / "scripts" / "w7d7_acceptance_check.py"
)
acc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acc)


# ---------- 成本线 ----------


def _cost_item(cost: float, hit: float, cheap: bool) -> dict:
    return {
        "per_1000_cost_units": cost,
        "cache_hit_rate": hit,
        "by_tier": {"cheap": {}, "primary": {}} if cheap else {"primary": {}},
    }


def _cost_payload(base: float = 1754.56, combo: float = 626.52, agree: float = 1.0) -> dict:
    return {
        "scenarios": [
            _cost_item(base, 0.0, False),
            _cost_item(base * 0.385, 0.0, True),
            _cost_item(base * 0.722, 0.284, False),
            _cost_item(combo, 0.284, True),
        ],
        "routing_agreement": {"total": 20, "agreement": agree, "mismatches": []},
        "unpriced_items": [{"item": "向量库"}, {"item": "rerank"}],
    }


def test_cost_passes_on_real_shape():
    ok, brief, checks = acc.judge_cost(_cost_payload())
    assert ok, checks
    assert "降 64" in brief


def test_cost_fails_when_saving_is_too_small():
    # 只省 30%，不到 60% 的门槛，不能判 PASS
    ok, _, _ = acc.judge_cost(_cost_payload(combo=1754.56 * 0.7))
    assert not ok


def test_cost_fails_when_unpriced_items_missing():
    payload = _cost_payload()
    payload["unpriced_items"] = []
    ok, _, checks = acc.judge_cost(payload)
    assert not ok
    assert any("未计入项" in c for c in checks)


def test_cost_fails_when_routing_disagrees():
    ok, _, _ = acc.judge_cost(_cost_payload(agree=0.95))
    assert not ok


def test_cost_scenario_lookup_is_explicit_about_ambiguity():
    # 两条一模一样的基线 → 特征不唯一，宁可报错也不猜
    payload = {"scenarios": [_cost_item(100, 0.0, False), _cost_item(100, 0.0, False)]}
    with pytest.raises(KeyError):
        acc._cost_scenario(payload, want_cache=False, want_cheap=False)


# ---------- 微调线 ----------


def _lora_payload(ratio: float = 0.125, weak_mse: float = 190.111) -> dict:
    return {
        "results": [
            {"name": "全量微调（训练整个 ΔW）", "trainable_params": 4096, "eval_mse_after": 0.000146},
            {"name": "LoRA r=4（只训练 B@A）", "trainable_params": int(4096 * ratio), "eval_mse_after": 0.000106},
            {"name": "LoRA r=1（只训练 B@A）", "trainable_params": 128, "eval_mse_after": weak_mse},
        ]
    }


def test_lora_passes_on_real_shape():
    ok, _, checks = acc.judge_lora(_lora_payload())
    assert ok, checks


def test_lora_fails_when_rank_starved_run_is_not_worse():
    # r=1 和 r=4 一样好 → 「秩不足学不全」这个对照没成立，不能算证据
    ok, _, _ = acc.judge_lora(_lora_payload(weak_mse=0.000106))
    assert not ok


def test_lora_fails_when_parameter_saving_is_marginal():
    ok, _, _ = acc.judge_lora(_lora_payload(ratio=0.5))
    assert not ok


# ---------- 决策线 ----------


def _decision_payload(agreement: float = 1.0, ft: int = 1, caveat: int = 1) -> dict:
    return {
        "scenarios": 12,
        "agreement": agreement,
        "evidence_coverage": 1.0,
        "finetune_recommendations": ft,
        "finetune_with_caveat": caveat,
        "evidence_index_size": 10,
    }


def test_decision_passes_on_real_shape():
    ok, brief, _ = acc.judge_decision(_decision_payload())
    assert ok
    assert "12/12" in brief


def test_decision_fails_when_finetune_becomes_default():
    # 微调推荐 5/12 → 「微调不是默认解」被推翻
    ok, _, _ = acc.judge_decision(_decision_payload(ft=5, caveat=5))
    assert not ok


def test_decision_fails_when_agreement_drops():
    ok, _, _ = acc.judge_decision(_decision_payload(agreement=0.9167))
    assert not ok


# ---------- Agent 线 ----------


def _agent_payload(finish: list[str] | None = None) -> dict:
    trace = [
        {"step": 1, "tool": "calculator", "args": {"expr": "128*4"}, "result": "CALC=512"},
        {"step": 2, "tool": "policy_lookup", "args": {"key": "截止日"}, "result": "值=每月 25 日"},
        {"step": 3, "tool": "summarize", "args": {"text": "x"}, "result": "回答：ok"},
    ]
    reasons = finish or [
        "completed",
        "completed",
        "completed",
        "max_steps",
        "timeout",
        "tool_error",
        "completed",
        "tool_error",
    ]
    return {
        "scenarios": [
            {"id": f"S{i + 1}", "finish_reason": r, "trace": trace if i == 0 else []}
            for i, r in enumerate(reasons)
        ],
        "resume": {"finish_reason": "completed", "steps": 3, "checkpoints": ["tools"] * 3},
        "legacy_crosscheck": [
            {"id": "S1", "graph": "completed", "legacy": "completed", "same_tools": True},
            {"id": "S2", "graph": "completed", "legacy": "completed", "same_tools": True},
        ],
    }


def _spans(broken_parent: bool = False) -> list[dict]:
    root = {"scenario": "S1", "name": "agent.run", "span_id": "root1", "parent_span_id": None}
    kids = [
        {
            "scenario": "S1",
            "name": f"agent.tool.{tool}",
            "span_id": f"k{i}",
            "parent_span_id": "other" if broken_parent else "root1",
        }
        for i, tool in enumerate(["calculator", "policy_lookup", "summarize"])
    ]
    return [root, *kids]


def test_agent_passes_on_real_shape():
    ok, brief, checks = acc.judge_agent(_agent_payload(), {"spans": _spans()})
    assert ok, checks
    assert "S1 span 4 条" in brief


def test_agent_fails_when_child_span_parent_is_wrong():
    # 子 span 挂到别的父上 → 证据链断了，不能算「父子层级已验」
    ok, _, _ = acc.judge_agent(_agent_payload(), {"spans": _spans(broken_parent=True)})
    assert not ok


def test_agent_fails_when_spans_missing():
    ok, _, _ = acc.judge_agent(_agent_payload(), {"spans": []})
    assert not ok


def test_agent_fails_when_resume_does_not_finish():
    payload = _agent_payload()
    payload["resume"]["finish_reason"] = "max_steps"
    ok, _, _ = acc.judge_agent(payload, {"spans": _spans()})
    assert not ok


def test_agent_fails_when_guard_reasons_not_covered():
    # 只剩 completed，三条守卫没触发过 → 没验到守卫
    ok, _, _ = acc.judge_agent(_agent_payload(finish=["completed"] * 8), {"spans": _spans()})
    assert not ok


# ---------- 报告清单 ----------


def test_all_declared_reports_exist():
    missing = [rel for rel in acc.REPORTS.values() if not (ROOT / rel).exists()]
    assert not missing, f"正本报告缺失：{missing}"


def test_not_reproducible_items_have_pointers():
    for name, why, where in acc.NOT_REPRODUCIBLE:
        assert name and why and where, f"{name} 缺少边界说明或指针"
