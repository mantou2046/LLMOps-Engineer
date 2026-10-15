#!/usr/bin/env python
"""W2 D10 · fallback 降级路径的 mock 验收（无需真 key、无需 vLLM）。

覆盖四条：
  1. 非流式：primary 重试耗尽 → 切 fallback 成功，degraded=True / degraded_from="primary"
  2. 流式：首 token 逼不出 → 切 fallback，degraded 透传到 ChatStream
  3. fallback 关闭（LLM_FALLBACK_ENABLED=0）→ 不降级，如实抛 UpstreamError
  4. 显式 tier="cheap" → 不走降级（降级只对默认 primary 生效）

跑法（在 app/ 同级，确保能 import llm）：
    LLM_MODE=mock MOCK_FAIL_N_TIMES=2 LLM_MAX_ATTEMPTS=2 \
        python scripts/test_fallback_mock.py

⚠️ mock 的故障计数器是**进程级全局**（_mock_calls），所以每个用例必须**独立进程**跑，
   否则前一个用例把计数器用掉、后一个用例观测不到失败。本脚本用 subprocess 自派发。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _run_case(name: str, env_extra: dict, code: str) -> tuple[bool, str]:
    """在一个干净子进程里跑一小段验收代码，回传 (是否通过, 输出)。"""
    env = dict(os.environ)
    env.update({"LLM_MODE": "mock", "PYTHONPATH": str(APP_DIR)})
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=120,
    )
    ok = proc.returncode == 0
    out = (proc.stdout or "") + (proc.stderr or "")
    return ok, f"[{name}] {'PASS' if ok else 'FAIL'}\n{out.strip()}"


CASE_NONSTREAM = """
import llm
assert llm.LLM_MODE == "mock", llm.LLM_MODE
assert llm._fallback_ready(), "fallback 应就绪（mock）"
out = llm.chat(messages=[{"role":"user","content":"hi"}], tier_name="primary")
assert out.degraded is True, f"应降级，实际 degraded={out.degraded}"
assert out.degraded_from == "primary", out.degraded_from
assert out.tier == "fallback", f"tier 应为 fallback，实际 {out.tier}"
assert out.content, "降级后应有内容"
print("OK nonstream: tier=%s degraded=%s from=%s attempts=%d" % (
    out.tier, out.degraded, out.degraded_from, out.attempts))
"""

CASE_STREAM = """
import llm
assert llm._fallback_ready()
st = llm.open_stream(messages=[{"role":"user","content":"hi"}], tier_name="primary")
assert st.degraded is True, f"流式应降级，实际 {st.degraded}"
assert st.degraded_from == "primary", st.degraded_from
assert st.tier == "fallback", st.tier
text = "".join(st.deltas)
assert text, "降级后流应有内容"
print("OK stream: tier=%s degraded=%s from=%s chars=%d" % (
    st.tier, st.degraded, st.degraded_from, len(text)))
"""

CASE_DISABLED = """
import llm
assert not llm._fallback_ready(), "fallback 应被开关关闭"
try:
    llm.chat(messages=[{"role":"user","content":"hi"}], tier_name="primary")
    raise SystemExit("FAIL: 本应抛 UpstreamError，却成功返回")
except llm.UpstreamError as exc:
    print("OK disabled: 如实抛出 UpstreamError ->", exc.message[:60])
"""

CASE_EXPLICIT_TIER = """
import llm
# 显式要 cheap 档：即使它失败也不该切 fallback（降级只对默认 primary 生效）
try:
    llm.chat(messages=[{"role":"user","content":"hi"}], tier_name="cheap")
    raise SystemExit("FAIL: 本应抛 UpstreamError")
except llm.UpstreamError:
    print("OK explicit-tier: cheap 失败未触发降级（符合设计）")
"""


def main() -> int:
    base = {"MOCK_FAIL_N_TIMES": "2", "LLM_MAX_ATTEMPTS": "2"}
    cases = [
        ("非流式降级", dict(base), CASE_NONSTREAM),
        ("流式降级", dict(base), CASE_STREAM),
        ("fallback 关闭", {**base, "LLM_FALLBACK_ENABLED": "0"}, CASE_DISABLED),
        ("显式 tier 不降级", dict(base), CASE_EXPLICIT_TIER),
    ]
    all_ok = True
    for name, env_extra, code in cases:
        ok, out = _run_case(name, env_extra, code)
        print(out)
        print("-" * 60)
        all_ok = all_ok and ok
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
