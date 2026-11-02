"""W5 D3 · 可控假上游（OpenAI 兼容），用来把 LLM 指标「量准」

为什么需要它 —— 这是 D3 全部结论的前提：

  D3 要验证的是**指标口径对不对**（TTFT 是不是只算首 token、成本是不是按
  token 计价、缓存命中到底记不记账）。这类验证必须满足一个条件：
  **真值已知**。真实上游给不出真值 —— 它今天首包 300ms、明天 3s，
  还可能被免费池 429（W4 D5 实测过 429 会伪装成缓存失效、fallback、
  token 数异常，看起来像四种不同的 bug）。

  所以这里起一个「会按剧本演出」的上游：首包固定等 300ms、之后每 40ms
  吐一段、token 数写死 100/50。于是「TTFT 应该是 0.3 秒出头」这句话
  可以被断言，而不是「大概几百毫秒吧」。

三条设计约束：

  1) **token 数写死，不按文本长度估算**。
     成本指标要能算，前提是 usage 确定。按长度估算的话，每次请求的
     prompt_tokens 都不同，成本就没法手算核对，只能「看着差不多」——
     那就等于没验证。

  2) **TTFT 与生成时间分开两个旋钮**（FAKE_FIRST_TOKEN_MS / FAKE_CHUNK_MS）。
     这是 D3 最核心的一条口径：TTFT 只量「到第一个 token」，不含后面
     吐完的时间。两个旋钮分开，才能证明指标没把两者加在一起。

  3) **故障注入按「第 N 个请求」计数，不按概率**。
     按概率注入的话，每次跑出来的错误率都不一样，手册里就写不出确定数字。
     按序号注入 → 「打 12 个请求、每 4 个坏 1 个」= 错误率必然是 0.25。

用法（在仓库根，终端 B）：
  D:\\python314\\python.exe scripts/fake_upstream.py
  D:\\python314\\python.exe scripts/fake_upstream.py --port 4010 --first-token-ms 300
  D:\\python314\\python.exe scripts/fake_upstream.py --fail-every 4      # 每 4 个请求坏 1 个

端点：
  POST /v1/chat/completions   OpenAI 兼容（stream=true 走 SSE）
  GET  /v1/models             给 LiteLLM 探活/列举用
  GET  /health                探活
  GET  /stats                 本进程累计计数（自己核对用了多少次，不依赖网关口径）

⚠️ 这是**教学用的假上游**，只在 127.0.0.1 上监听，别拿去做任何真实用途。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-upstream (W5 D3)", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# 剧本参数（全部可用环境变量覆盖，命令行优先级更高）
# ---------------------------------------------------------------------------
FIRST_TOKEN_MS = int(os.getenv("FAKE_FIRST_TOKEN_MS", "300"))
CHUNK_MS = int(os.getenv("FAKE_CHUNK_MS", "40"))
CHUNKS = int(os.getenv("FAKE_CHUNKS", "5"))
FAIL_EVERY = int(os.getenv("FAKE_FAIL_EVERY", "0"))  # 0 = 不注入
PROMPT_TOKENS = int(os.getenv("FAKE_PROMPT_TOKENS", "100"))
COMPLETION_TOKENS = int(os.getenv("FAKE_COMPLETION_TOKENS", "50"))

# 累计计数：自己数一遍，用来跟网关/Prometheus 的口径对账
_stats: dict[str, Any] = {"total": 0, "ok": 0, "failed": 0, "stream": 0, "nonstream": 0}


def _next_seq() -> int:
    """第几个请求（从 1 开始）。故障注入按它算，保证每次跑的数字一样。"""
    _stats["total"] += 1
    return int(_stats["total"])


def _should_fail(seq: int) -> bool:
    return FAIL_EVERY > 0 and seq % FAIL_EVERY == 0


def _usage() -> dict:
    """固定 usage —— 成本可手算，这是本脚本存在的理由之一。"""
    return {
        "prompt_tokens": PROMPT_TOKENS,
        "completion_tokens": COMPLETION_TOKENS,
        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
    }


def _chunk_text(seq: int) -> str:
    return f"这是假上游第 {seq} 次调用的回答，用于验证 LLM 指标口径。"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "stats": _stats}


@app.get("/stats")
def stats() -> dict:
    return {
        **_stats,
        "script": {
            "first_token_ms": FIRST_TOKEN_MS,
            "chunk_ms": CHUNK_MS,
            "chunks": CHUNKS,
            "fail_every": FAIL_EVERY,
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
        },
    }


@app.get("/v1/models")
def models() -> dict:
    """LiteLLM 启动/健康检查会来问一句有哪些模型 —— 给个假清单即可。"""
    return {
        "object": "list",
        "data": [{"id": "fake-model", "object": "model", "owned_by": "fake-upstream"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    seq = _next_seq()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    stream = bool(body.get("stream", False))

    # ---- 故障注入：按序号，不按概率（见文件头约束 3）-----------------------
    if _should_fail(seq):
        _stats["failed"] += 1
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"fake upstream 注入的故障（第 {seq} 个请求，每 {FAIL_EVERY} 个坏 1 个）",
                    "type": "server_error",
                    "code": "injected_failure",
                }
            },
        )

    _stats["ok"] += 1
    _stats["stream" if stream else "nonstream"] += 1
    text = _chunk_text(seq)
    model = str(body.get("model") or "fake-model")
    cmpl_id = f"chatcmpl-fake-{uuid.uuid4().hex[:16]}"

    # ---- 非流式 -----------------------------------------------------------
    if not stream:
        # 整段等待 = 首包 + 生成，模拟真实模型「一次性返回」的耗时
        time.sleep((FIRST_TOKEN_MS + CHUNK_MS * CHUNKS) / 1000.0)
        return JSONResponse(
            content={
                "id": cmpl_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _usage(),
            }
        )

    # ---- 流式 -------------------------------------------------------------
    def _gen() -> Iterator[bytes]:
        # 第一段：等 FAKE_FIRST_TOKEN_MS 再吐 —— 这段等待就是 TTFT 的全部来源
        time.sleep(FIRST_TOKEN_MS / 1000.0)
        pieces = [text[i : i + 8] for i in range(0, len(text), 8)] or [""]
        for idx, piece in enumerate(pieces[:CHUNKS] if CHUNKS > 0 else pieces):
            frame = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()
            time.sleep(CHUNK_MS / 1000.0)
        # 收尾帧：finish_reason=stop
        tail = {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode()
        # ⚠️ usage 只在**流末**这一帧出现（stream_options.include_usage 的语义）。
        #    LiteLLM 就是靠这一帧拿到 token 数去算成本的 —— 少这一帧，成本恒为 0。
        usage_frame = {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": _usage(),
        }
        yield f"data: {json.dumps(usage_frame, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


def main() -> int:
    ap = argparse.ArgumentParser(description="W5 D3 可控假上游")
    ap.add_argument("--port", type=int, default=4010)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--first-token-ms", type=int, default=None, help="首包等待（决定 TTFT）")
    ap.add_argument("--chunk-ms", type=int, default=None, help="相邻 chunk 间隔")
    ap.add_argument("--chunks", type=int, default=None, help="吐几段")
    ap.add_argument("--fail-every", type=int, default=None, help="每 N 个请求注入 1 个 500；0=不注入")
    args = ap.parse_args()

    # 命令行覆盖模块级参数（argparse 之后再赋值，保持函数内读取时是最新值）
    global FIRST_TOKEN_MS, CHUNK_MS, CHUNKS, FAIL_EVERY
    if args.first_token_ms is not None:
        FIRST_TOKEN_MS = args.first_token_ms
    if args.chunk_ms is not None:
        CHUNK_MS = args.chunk_ms
    if args.chunks is not None:
        CHUNKS = args.chunks
    if args.fail_every is not None:
        FAIL_EVERY = args.fail_every

    import uvicorn

    print(
        f"fake upstream → http://{args.host}:{args.port}/v1\n"
        f"  首包 {FIRST_TOKEN_MS} ms · 每段 {CHUNK_MS} ms × {CHUNKS} 段 · "
        f"故障注入每 {FAIL_EVERY or '∞'} 个 1 次\n"
        f"  usage 固定 {PROMPT_TOKENS}/{COMPLETION_TOKENS} tokens",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
