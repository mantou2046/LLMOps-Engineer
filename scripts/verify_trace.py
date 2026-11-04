"""W5 D7 补证：验一条 trace 能不能真正落到 Langfuse（不经 HTTP，直接打观测层）。

为什么不用 `dev_app.ps1 -Mock` 验：脚本第 55 行把 OBSERVABILITY 硬置成 off，
--mock 是为了「不花钱」顺手把观测也关了 —— 用它验 trace 必然 0 条。

这只验「观测上报」这条边，不碰 LLM 上游：
  terminal ──trace_chat()──> Langfuse SDK ──OTLP/HTTP──> worker ──> ClickHouse
"""

import os
import sys
import uuid

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + r"\app"
sys.path.insert(0, APP_DIR)

# 本地服务不走代理（本机代理会劫持 localhost:3000 的请求）
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["OBSERVABILITY"] = "langfuse"

import observability as obs  # noqa: E402

print("status =", obs.status())

rid = f"w5d7-verify-{uuid.uuid4().hex[:8]}"
with obs.trace_chat(
    request_id=rid,
    model="gpt-4o-mini",
    tier="fast",
    mode="mock",
    messages=[{"role": "user", "content": "W5 D7 trace 链补证：这条能进 ClickHouse 吗？"}],
    structured=False,
    stream=False,
) as rec:
    rec["usage"] = {"prompt_tokens": 12, "completion_tokens": 8}
    rec["content"] = "trace chain verified"
    rec["finish_reason"] = "stop"

obs.flush()
print("flushed, request_id =", rid)
