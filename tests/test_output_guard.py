from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from main import app
from output_guard import SAFE_FALLBACK, inspect_output

client = TestClient(app)


def test_valid_json_passes():
    result = inspect_output({"answer": "答案", "citations": ["资料1"]})
    assert result.output == {"answer": "答案", "citations": ["资料1"]}
    assert result.degraded is False
    assert result.toxicity_status == "not_checked"
    assert result.grounding_status == "not_checked"


def test_invalid_json_repairs_once():
    calls = []

    def repair(raw, error):
        calls.append((raw, error))
        return {"answer": "修复后的答案", "citations": []}

    result = inspect_output('{"answer": ["类型错误"]}', repair=repair)
    assert result.output["answer"] == "修复后的答案"
    assert result.repaired is True
    assert result.degraded is True
    assert result.reasons == ["format_repaired"]
    assert len(calls) == 1


def test_repair_failure_returns_schema_valid_fallback():
    result = inspect_output(
        '{"answer": ["类型错误"]}',
        repair=lambda _raw, _error: "仍然不是 JSON",
    )
    assert result.output == SAFE_FALLBACK
    assert result.degraded is True
    assert result.reasons == ["format_repair_failed"]


def test_personal_information_is_redacted_in_answer_and_citation():
    result = inspect_output(
        {
            "answer": "联系 demo.user@example.com 或 13812345678",
            "citations": ["demo.user@example.com"],
        }
    )
    assert "demo.user@example.com" not in str(result.output)
    assert "13812345678" not in str(result.output)
    assert result.redacted_types == ["email", "phone_cn"]


def test_harmful_content_is_blocked():
    result = inspect_output(
        {"answer": "你真差劲，去死吧。", "citations": []},
        toxicity_judge=lambda _text: "block",
    )
    assert result.output == SAFE_FALLBACK
    assert result.reasons == ["harmful_content"]


def test_toxicity_checker_error_fails_closed():
    def broken(_text):
        raise TimeoutError("checker timed out")

    result = inspect_output(
        {"answer": "普通回答", "citations": []},
        toxicity_judge=broken,
    )
    assert result.output == SAFE_FALLBACK
    assert result.toxicity_status == "error"
    assert result.reasons == ["toxicity_check_error"]


def test_missing_context_is_not_marked_as_grounded():
    result = inspect_output(
        {"answer": "一个数字是 999。", "citations": []},
        grounding_judge=lambda _answer, _context: True,
    )
    assert result.grounding_status == "not_checked"


def test_unsupported_claim_returns_fallback():
    result = inspect_output(
        {"answer": "资料证明数字是 999。", "citations": []},
        context="资料没有提供这个数字。",
        grounding_judge=lambda answer, context: "999" in context,
    )
    assert result.output == SAFE_FALLBACK
    assert result.grounding_status == "unsupported"
    assert result.reasons == ["unsupported_claim"]


def test_endpoint_redacts_structured_output(monkeypatch):
    monkeypatch.setattr("main.OUTPUT_GUARD_ENABLED", True)
    monkeypatch.setattr(
        "main.llm.chat",
        lambda **_kwargs: SimpleNamespace(
            request_id="test",
            content={"answer": "联系 demo.user@example.com", "citations": []},
            model="mock",
            tier="primary",
            mode="mock",
            usage={},
            attempts=1,
            retries=0,
            latency_ms=1.0,
            structured=True,
            parse_failed=False,
            finish_reason="stop",
            degraded=False,
            degraded_from=None,
            prompt_version="",
            prompt_hash="",
        ),
    )
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "给我一个 JSON 回答"}],
            "structured": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"]["answer"] == "联系 [已遮盖:email]"
    assert body["output_guard"]["redacted_types"] == ["email"]


def test_endpoint_repairs_only_once(monkeypatch):
    monkeypatch.setattr("main.OUTPUT_GUARD_ENABLED", True)
    calls = []
    contents = [
        {"answer": ["错的类型"], "citations": []},
        {"answer": "修复完成", "citations": []},
    ]

    def fake_chat(**_kwargs):
        calls.append(1)
        return SimpleNamespace(
            request_id="test",
            content=contents.pop(0),
            model="mock",
            tier="primary",
            mode="mock",
            usage={},
            attempts=1,
            retries=0,
            latency_ms=1.0,
            structured=True,
            parse_failed=False,
            finish_reason="stop",
            degraded=False,
            degraded_from=None,
            prompt_version="",
            prompt_hash="",
        )

    monkeypatch.setattr("main.llm.chat", fake_chat)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "给我一个 JSON 回答"}],
            "structured": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["content"]["answer"] == "修复完成"
    assert response.json()["output_guard"]["repaired"] is True
    assert len(calls) == 2


def test_guarded_streaming_is_rejected_before_upstream(monkeypatch):
    monkeypatch.setattr("main.OUTPUT_GUARD_ENABLED", True)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "给我一个答案"}],
            "stream": True,
        },
    )
    assert response.status_code == 422
    assert "流式" in response.json()["detail"]
