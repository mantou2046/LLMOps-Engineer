"""端到端集成测：TestClient 发真 POST /chat，走完整 HTTP 链路。

LLM_MODE=mock（conftest 里已设），不起 uvicorn、不联网 —— 但协议层 /
路由 / SSE 组帧 / 错误翻译都是真代码。验证「一个请求从 HTTP 进来、
经过分流、到 mock 上游、再带路由信息回到客户端」全链路通。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_chat_plain():
    """普通问题 → 200，body 带 routing 现场与 mock 文本。"""
    r = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "这个字段怎么填"}], "stream": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["upstream_mode"] == "mock"
    assert body["tier"] in ("primary", "cheap")
    assert isinstance(body["content"], str) and len(body["content"]) > 0
    assert "routing" in body  # W4 D6：分流现场必须回显


def test_chat_response_headers_carry_route():
    """路由结论写进响应头（X-Route-Tier / X-Route-Score）。"""
    r = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "请对比两个方案差异"}], "stream": False},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Route-Tier") == "primary"  # 强词「对比」→ primary
    assert r.headers.get("X-Route-Score") is not None


def test_chat_stream_sse():
    """流式 → SSE，能收到 meta / delta / done 三类事件。"""
    with client.stream(
        "POST",
        "/chat",
        json={"messages": [{"role": "user", "content": "讲个故事"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: meta" in body
    assert "event: delta" in body
    assert "event: done" in body


def test_chat_structured_returns_json():
    """structured=True → content 是 dict（协议层拼回并 parse）。"""
    r = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "返回一个 JSON 对象"}],
            "structured": True,
        },
    )
    assert r.status_code == 200
    assert isinstance(r.json()["content"], dict)


def test_chat_invalid_body_422():
    """缺 messages → 422（FastAPI 校验拦截，不是 500 也不是 400）。

    ⚠️ Pydantic 校验失败默认返回 422（Unprocessable Content），不是 400。
    想验证「协议层拦截」用 422，别写成 400 —— 400 只在显式抛 HTTPException 时出现。
    """
    r = client.post("/chat", json={"stream": False})
    assert r.status_code == 422
