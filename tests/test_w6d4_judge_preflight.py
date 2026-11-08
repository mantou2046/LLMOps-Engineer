"""换裁判门禁的纯函数测试：不联网、不读凭据。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "judge_preflight", ROOT / "scripts" / "w6d4_judge_preflight.py"
)
jp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jp)


def test_strip_fence_plain():
    assert jp.strip_fence('  {"a": 1}  ') == '{"a": 1}'


def test_strip_fence_json_code_block():
    assert jp.strip_fence('```json\n{"statements": ["x"]}\n```') == '{"statements": ["x"]}'


def test_parse_json_object():
    ok, keys = jp.parse_json('```json\n{"statements": ["x"]}\n```')
    assert ok is True
    assert keys == ["statements"]


def test_parse_json_rejects_prose_and_list():
    assert jp.parse_json("让我分析一下这段回答")[0] is False
    assert jp.parse_json('["a", "b"]')[0] is False


def test_summarize_codes():
    assert jp.summarize_codes([200, 200, 403]) == {"200": 2, "403": 1}


def _long(json_ok=True, thinking=False, sec=2.5):
    return {"http": 200, "sec": sec, "json_ok": json_ok, "thinking": thinking}


def test_gate_pass():
    ok, reasons = jp.evaluate_gate(
        [_long(), _long(), _long()], [200] * 12, min_json_ok=2,
        min_burst_success=1.0, max_sec=30,
    )
    assert ok is True
    assert reasons == []


def test_gate_fails_when_burst_all_403():
    ok, reasons = jp.evaluate_gate(
        [_long(), _long(), _long()], [403] * 12, min_json_ok=2,
        min_burst_success=1.0, max_sec=30,
    )
    assert ok is False
    assert any("连发成功 0/12" in r for r in reasons)
    assert any("403" in r for r in reasons)


def test_gate_fails_on_partial_429():
    ok, reasons = jp.evaluate_gate(
        [_long(), _long(), _long()], [200] * 10 + [429] * 2,
        min_json_ok=2, min_burst_success=1.0, max_sec=30,
    )
    assert ok is False
    assert any("10/12" in r for r in reasons)


def test_gate_fails_on_thinking_prefix():
    ok, reasons = jp.evaluate_gate(
        [_long(thinking=True), _long(), _long()], [200] * 12,
        min_json_ok=2, min_burst_success=1.0, max_sec=30,
    )
    assert ok is False
    assert any("思考痕迹" in r for r in reasons)


def test_gate_fails_when_long_prompt_not_json():
    ok, reasons = jp.evaluate_gate(
        [_long(json_ok=False), _long(json_ok=False), _long()], [200] * 12,
        min_json_ok=2, min_burst_success=1.0, max_sec=30,
    )
    assert ok is False
    assert any("干净 JSON 1/3" in r for r in reasons)


def test_gate_fails_on_slow_response():
    ok, reasons = jp.evaluate_gate(
        [_long(sec=45.0), _long(), _long()], [200] * 12,
        min_json_ok=2, min_burst_success=1.0, max_sec=30,
    )
    assert ok is False
    assert any("超过 30" in r for r in reasons)
