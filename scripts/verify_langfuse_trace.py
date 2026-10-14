#!/usr/bin/env python3
"""W2 D9 端到端验收：用真实 observability 模块把一次「假 chat」打进真实 Langfuse，
然后直连 ClickHouse 确认 span + generation 都落了库。

背景（v4 实测差异）：
  - Langfuse v4 官方 `langfuse:4` 镜像跑在 **events_only** 模式：公共 REST 查询 API
    （/api/public/traces、/api/public/observations）一律 404，但 OTLP 上报 + UI 正常。
  - 所以程序化验收不能走公共 API，改为直连 ClickHouse（observations 表就是落库处）。
    UI 面板则照常能看。

用法：
  python scripts/verify_langfuse_trace.py \
      --public-key pk-lf-... --secret-key sk-lf-... \
      --clickhouse-url http://localhost:8123 \
      --ch-user clickhouse --ch-password <...>

退出码：0 = span+generation 均落库；非 0 = 失败。
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import uuid
import urllib.parse
from urllib.error import URLError
from urllib.request import Request, urlopen


def _post_trace(host: str, public_key: str, secret_key: str) -> str:
    """用真实 observability 模块发一条 trace，返回唯一 request_id。"""
    # ⚠️ 必须在 import observability 之前设好环境变量：模块在 import 时读取 OBSERVABILITY / KEY / HOST
    os.environ["OBSERVABILITY"] = "langfuse"
    os.environ["LANGFUSE_HOST"] = host
    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    os.environ["LANGFUSE_SAMPLE_RATE"] = "1.0"

    app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
    sys.path.insert(0, os.path.abspath(app_dir))
    import observability  # noqa: E402  — 故意在设好 env 之后才 import

    rid = f"verify-{uuid.uuid4().hex[:8]}"
    # input 里塞进唯一 marker，ClickHouse 侧按它精确定位「就是我发的这条」
    messages = [{"role": "user", "content": f"ping [verify {rid}]"}]
    with observability.trace_chat(
        request_id=rid,
        model="gpt-4o-mini",
        tier="primary",
        mode="mock",
        messages=messages,
        structured=False,
        stream=False,
    ) as rec:
        rec["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        rec["content"] = "pong"
        rec["finish_reason"] = "stop"
    observability.flush()
    return rid


def _ch(ch_url: str, user: str, password: str, sql: str) -> str:
    """直连 ClickHouse HTTP 接口执行查询，返回 TabSeparated 文本。"""
    url = ch_url.rstrip("/") + "/?" + urllib.parse.urlencode({"query": sql})
    req = Request(url, data=b"")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode()


def _v2_observations(host: str, public_key: str, secret_key: str, limit: int = 10):
    """v4 的公共查询 API 在 /api/public/**v2**/observations（v1 路径已 404）。
    这是 UI 面板的数据源 —— 用它能把「面板真能看到」验证到根上。"""
    import json

    url = f"{host.rstrip('/')}/api/public/v2/observations?limit={limit}"
    req = Request(url)
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:3000")
    ap.add_argument("--public-key", required=True)
    ap.add_argument("--secret-key", required=True)
    ap.add_argument("--clickhouse-url", default="http://localhost:8123")
    ap.add_argument("--ch-user", required=True)
    ap.add_argument("--ch-password", required=True)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    rid = _post_trace(args.host, args.public_key, args.secret_key)
    print(f"[verify] 已发送 trace request_id={rid}，轮询 ClickHouse 等落库…")

    # v4 events_only 模式：trace 实际落在 events_full（observations/traces 是旧模式表，不写）。
    sql = (
        "SELECT name, type, provided_model_name, metadata_names, metadata_values, "
        "provided_usage_details, provided_cost_details, calculated_total_cost, "
        "level, status_message "
        f"FROM events_full WHERE input LIKE '%{rid}%' ORDER BY start_time ASC LIMIT 10"
    )

    rows: list[str] = []
    for i in range(args.attempts):
        time.sleep(args.interval)
        try:
            out = _ch(args.clickhouse_url, args.ch_user, args.ch_password, sql)
        except URLError as exc:
            print(f"[verify] 第 {i + 1} 次查询失败：{exc}")
            continue
        rows = [ln for ln in out.strip().split("\n") if ln and not ln.startswith("name\t")]
        if rows:
            break
        print(f"[verify] 第 {i + 1} 次：尚未落库，继续等…")

    if not rows:
        print(f"[verify] ❌ 失败：{args.attempts} 次轮询后仍未在 events_full 找到 {rid}")
        return 4

    has_span = any(r.split("\t")[0] == "chat" for r in rows)
    has_gen = any(r.split("\t")[0].startswith("chat.") for r in rows)
    has_request_id = any("request_id" in r for r in rows)
    print(f"[verify] ✅ ClickHouse 落库成功，共 {len(rows)} 行：")
    for r in rows:
        print(f"    {r}")
    print(
        f"[verify]   has_span={has_span} has_generation={has_gen} "
        f"request_id_in_metadata={has_request_id}"
    )

    # 第二重：走 v4 公共 v2 查询 API（= UI 面板的数据源），证明「面板能看到」
    print("[verify] 校验 UI 数据源（GET /api/public/v2/observations）…")
    try:
        data = _v2_observations(args.host, args.public_key, args.secret_key)
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] ⚠️ v2 observations 查询失败：{exc}")
        data = None
    if data is not None:
        obs = data.get("data", [])
        trace_ids = {o.get("traceId") for o in obs if o.get("name") == "chat"}
        root = [o for o in obs if o.get("name") == "chat" and o.get("isRootObservation")]
        gen = [o for o in obs if str(o.get("name", "")).startswith("chat.")]
        print(
            f"[verify]   面板数据源可见 {len(obs)} 条 observation；"
            f"根 span {len(root)} 个、generation {len(gen)} 个"
        )
        for o in obs[:2]:
            print(
                f"     - {o.get('type')} name={o.get('name')} trace={o.get('traceId')} "
                f"root={o.get('isRootObservation')}"
            )
        if root and gen:
            print("[verify] ✅ UI 面板数据源确认有 trace（span + generation）")
        else:
            print("[verify] ⚠️ 面板数据源里没看到成对 span/generation")

    if not (has_span and has_gen):
        print("[verify] ⚠️ span/generation 不全——层级可能没对")
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
