"""W7 D5 · 策略选择器单测：证据纪律、规则优先级、场景集一致率。"""

from __future__ import annotations

import json
from pathlib import Path

import decision
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "evals" / "decision" / "scenarios.jsonl"


def _features(**kw) -> decision.TaskFeatures:
    base = dict(name="测试任务")
    base.update(kw)
    return decision.TaskFeatures(**base)


# ---- 证据纪律 --------------------------------------------------------------
def test_evidence_index_is_complete_and_nonempty():
    assert len(decision.EVIDENCE_INDEX) == 10
    assert all(k and v for k, v in decision.EVIDENCE_INDEX.items())


def test_validate_flags_missing_evidence():
    rec = decision.Recommendation("RAG", (), "理由", (), ())
    assert "结论没有证据指针" in decision.validate(rec)


def test_validate_flags_unknown_evidence_id():
    rec = decision.Recommendation("RAG", (), "理由", ("E-FAKE",), ())
    assert any("未知证据编号" in p for p in decision.validate(rec))


def test_validate_flags_finetune_without_caveat():
    rec = decision.Recommendation("微调", (), "理由", ("E-FT-1",), ())
    assert any("效果未量" in p for p in decision.validate(rec))


def test_validate_passes_a_well_formed_finetune_recommendation():
    rec = decision.decide(_features(behavior_unstable=True, has_labeled_samples=True))
    assert rec.primary == "微调"
    assert decision.validate(rec) == []
    assert any("效果未量" in c for c in rec.caveats)


# ---- 规则优先级 ------------------------------------------------------------
def test_changing_knowledge_beats_finetune_signals():
    """S10：知识会变 + 有样本 + 行为不稳，仍然必须判 RAG。"""
    rec = decision.decide(
        _features(
            knowledge_changes=True,
            behavior_unstable=True,
            has_labeled_samples=True,
        )
    )
    assert rec.primary == "RAG"
    assert "Prompt 结构校验" in rec.also


def test_traceability_forces_rag():
    assert decision.decide(_features(needs_traceability=True)).primary == "RAG"
    assert decision.decide(_features(private_knowledge=True)).primary == "RAG"


def test_no_samples_never_recommends_finetune():
    rec = decision.decide(_features(behavior_unstable=True, has_labeled_samples=False))
    assert rec.primary == "Prompt"
    assert "攒高质量样本" in rec.also


def test_simple_rules_go_to_prompt():
    rec = decision.decide(_features(rules_simple=True))
    assert rec.primary == "Prompt"
    assert "E-PROMPT-1" in rec.evidence


def test_decide_is_deterministic():
    f = _features(knowledge_changes=True, behavior_unstable=True)
    assert decision.decide(f) == decision.decide(f)


# ---- 固定场景集 ------------------------------------------------------------
def _load() -> list[dict]:
    return [
        json.loads(line)
        for line in SCENARIOS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_scenario_set_agreement_and_evidence_coverage():
    rows = _load()
    assert len(rows) == 12
    for s in rows:
        rec = decision.decide(decision.TaskFeatures(name=s["name"], **s["features"]))
        assert rec.primary == s["expected_primary"], s["id"]
        assert decision.validate(rec) == [], s["id"]
        assert rec.evidence, s["id"]


def test_finetune_is_not_the_default_answer():
    rows = _load()
    recs = [
        decision.decide(decision.TaskFeatures(name=s["name"], **s["features"]))
        for s in rows
    ]
    n_ft = sum(1 for r in recs if r.primary == "微调")
    assert n_ft == 1, "12 条场景里微调只该出现 1 次"


@pytest.mark.parametrize("eid", sorted(decision.EVIDENCE_INDEX))
def test_every_evidence_id_points_to_a_real_report(eid):
    text = decision.EVIDENCE_INDEX[eid]
    assert "evals/" in text, f"{eid} 没有指向仓库报告"
