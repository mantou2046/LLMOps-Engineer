"""W6 D4 换裁判前的本地门禁：长 prompt JSON 体检 + 连发 burst 体检。

背景（2026-11-08）：D5 的 --preflight 只做 3 次单发，能拦「模型不通 / 格式不对」，
拦不住「单发可用但密集批量被限流」。当天实测 gpt-4.1-mini 长 prompt 单次出干净
JSON、burst 12/12 全 403 model_location_not_supported；deepseek-v4.1-flash 免费档
burst 429 rate_limit_exceeded。所以换任何裁判前必须跑本脚本，退出码 0 才允许改
RAGAS_JUDGE_MODEL。

用法（作品集仓根目录）：
    ./.venv-eval/Scripts/python.exe scripts/w6d4_judge_preflight.py \
        --model glm-5.3-flash --burst 12 --long-runs 3

环境变量（与 w6d4_ragas_eval.py 一致；脚本不读 .env、不落盘凭据）：
    RAGAS_JUDGE_BASE_URL   OpenAI 兼容网关 base URL
    LLM_PRIMARY_API_KEY    judge key
    RAGAS_JUDGE_MODEL      默认裁判，可用 --model 覆盖

退出码：0 通过 / 2 体检不通过 / 1 配置或参数错误
报告：默认写 evals/out/judge_preflight/<model>_<时间戳>.json（该目录被 gitignore）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "evals" / "out" / "judge_preflight"

TECH = (
    "检索增强生成系统把文档切成固定长度的块，为每块计算向量并写入向量索引；"
    "查询到来时先用同一模型把问题编码成向量，再做近邻搜索取回若干候选块，"
    "与关键词检索的结果按倒数排名融合后交给生成模型。切块粒度会同时影响召回率与精确度："
    "块太大容易混入无关内容，块太小会把一句完整的话拆开，中文尤其明显，因为中文没有空格边界。"
    "评估时把标准答案拆成原子断言，逐条判断能否从检索到的上下文中推出，得到忠实度；"
    "把检索结果与标准答案的要点比对，得到上下文召回；相关块是否排在前面决定上下文精确度；"
    "答案与问题的语义相似程度则由反推问题与余弦相似度衡量。"
    "这些指标互相牵制，单独看任何一个都容易被误导，需要放在同一批样本里一起读。"
)
LONG_PROMPT = (
    "你是评估裁判。下面是检索上下文和一段回答。请把回答拆成原子断言，"
    '只输出一个 JSON 对象，格式为 {"statements": ["断言1", "断言2"]}。'
    "不要解释、不要输出分析过程、不要写 Markdown 代码块。\n\n"
    "【检索上下文】\n" + (TECH * 9)[:1900] + "\n\n"
    "【回答】\n系统采用递归切分，块大小 400 字，重叠 50 字，"
    "并用倒数排名融合向量检索与关键词检索两路结果；生产环境需显式设置 probes 参数。\n"
)
SHORT_PROMPT = 'Return only JSON: {"ok": true}'
THINKING_MARKERS = ("<think>", "让我", "分析一下", "思考过程", "用户要求", "我需要")


def build_opener():
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    )
    return urllib.request.build_opener(*handlers)


def strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def parse_json(text: str) -> tuple[bool, list[str]]:
    try:
        obj = json.loads(strip_fence(text))
    except (json.JSONDecodeError, ValueError):
        return False, []
    if isinstance(obj, dict):
        return True, list(obj.keys())[:5]
    return False, []


def call(
    opener,
    base_url: str,
    key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    timeout: int,
) -> tuple[int, str, float]:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as exc:  # noqa: BLE001 - 网络层错误要当成体检失败，不中断整轮
        return 0, "ERR " + type(exc).__name__ + ": " + str(exc)[:100], time.time() - t0


def error_code(body: str) -> str:
    try:
        err = json.loads(body).get("error", {})
        return str(err.get("code") or err.get("message") or "")[:100]
    except (json.JSONDecodeError, AttributeError):
        return body[:100].replace("\n", " ")


def inspect_long(body: str, status: int, sec: float) -> dict:
    content, provider, cost = "", None, None
    if status == 200:
        try:
            obj = json.loads(body)
            content = obj["choices"][0]["message"].get("content") or ""
            provider = obj.get("provider")
            cost = (obj.get("usage") or {}).get("cost")
        except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
            content = ""
    ok, keys = parse_json(content)
    return {
        "http": status,
        "sec": round(sec, 2),
        "json_ok": ok and status == 200,
        "keys": keys,
        "chars": len(content),
        "thinking": any(m in content[:350] for m in THINKING_MARKERS),
        "provider": provider,
        "cost": cost,
        "err": error_code(body) if status != 200 else None,
    }


def summarize_codes(codes: list[int]) -> dict[str, int]:
    return {str(k): v for k, v in sorted(Counter(codes).items())}


def evaluate_gate(
    long_results: list[dict],
    burst_codes: list[int],
    min_json_ok: int,
    min_burst_success: float,
    max_sec: float,
) -> tuple[bool, list[str]]:
    """纯函数判定，便于单测。burst 有任何非 200 都算不稳定。"""
    reasons: list[str] = []
    clean = sum(
        1
        for r in long_results
        if r.get("json_ok") and not r.get("thinking") and r.get("sec", 999) <= max_sec
    )
    if clean < min_json_ok:
        reasons.append(
            f"长 prompt 干净 JSON {clean}/{len(long_results)}，低于要求 {min_json_ok}"
        )
    thinking = sum(1 for r in long_results if r.get("thinking"))
    if thinking:
        reasons.append(f"{thinking} 次输出带思考痕迹，ragas 解析会失败")
    total = len(burst_codes)
    ok = sum(1 for c in burst_codes if c == 200)
    ratio = ok / total if total else 0.0
    if ratio < min_burst_success:
        bad = summarize_codes([c for c in burst_codes if c != 200])
        reasons.append(f"连发成功 {ok}/{total}（{ratio:.0%}），非 200 分布 {bad}")
    if any(r.get("sec", 0) > max_sec for r in long_results):
        slow = [r["sec"] for r in long_results if r.get("sec", 0) > max_sec]
        reasons.append(f"有 {len(slow)} 次超过 {max_sec}s 上限：{slow}")
    return not reasons, reasons


def main() -> int:
    ap = argparse.ArgumentParser(description="换裁判前的长 prompt + burst 体检门禁")
    ap.add_argument("--model", default="", help="裁判 slug；不传用 RAGAS_JUDGE_MODEL")
    ap.add_argument("--base-url", default="", help="网关 base URL；不传用 RAGAS_JUDGE_BASE_URL")
    ap.add_argument("--long-runs", type=int, default=3, help="长 prompt 重复次数")
    ap.add_argument("--burst", type=int, default=12, help="短 prompt 连发次数")
    ap.add_argument(
        "--min-json-ok", type=int, default=None,
        help="长 prompt 至少几次干净 JSON；默认 min(2, --long-runs)",
    )
    ap.add_argument("--min-burst-success", type=float, default=1.0, help="连发 200 比例下限")
    ap.add_argument("--max-sec", type=float, default=30.0, help="单次耗时上限（秒）")
    ap.add_argument("--timeout", type=int, default=30, help="单次网络超时（秒）")
    ap.add_argument("--json-out", default="", help="报告路径；默认写 evals/out/judge_preflight/")
    args = ap.parse_args()

    base_url = args.base_url or os.environ.get("RAGAS_JUDGE_BASE_URL", "")
    key = os.environ.get("LLM_PRIMARY_API_KEY", "")
    model = args.model or os.environ.get("RAGAS_JUDGE_MODEL", "")
    if not (base_url and key and model):
        print("[config] 需要 base_url / key / model：设置环境变量或用参数补齐", file=sys.stderr)
        return 1
    if args.long_runs < 1 or args.burst < 1:
        print("[config] --long-runs 与 --burst 至少为 1", file=sys.stderr)
        return 1
    min_json_ok = (
        args.min_json_ok if args.min_json_ok is not None else min(2, args.long_runs)
    )
    if min_json_ok < 1 or min_json_ok > args.long_runs:
        print("[config] --min-json-ok 必须在 1..--long-runs 之间", file=sys.stderr)
        return 1

    opener = build_opener()
    print(
        f"[preflight] model={model} base={base_url} "
        f"long={args.long_runs} burst={args.burst}",
        flush=True,
    )
    long_results: list[dict] = []
    for i in range(args.long_runs):
        status, body, sec = call(
            opener, base_url, key, model,
            [{"role": "system", "content": "你只输出 JSON。"},
             {"role": "user", "content": LONG_PROMPT}],
            700, args.timeout,
        )
        r = inspect_long(body, status, sec)
        long_results.append(r)
        print(
            f"[long {i + 1}/{args.long_runs}] http={r['http']} json={r['json_ok']} "
            f"thinking={r['thinking']} sec={r['sec']} provider={r['provider']} "
            f"cost={r['cost']} err={r['err']}",
            flush=True,
        )

    burst_codes: list[int] = []
    burst_secs: list[float] = []
    for i in range(args.burst):
        status, body, sec = call(
            opener, base_url, key, model,
            [{"role": "user", "content": SHORT_PROMPT}], 40, args.timeout,
        )
        burst_codes.append(status)
        burst_secs.append(round(sec, 2))
        if status != 200:
            print(f"[burst {i + 1}/{args.burst}] http={status} {error_code(body)[:60]}", flush=True)
    codes = summarize_codes(burst_codes)
    print(
        f"[burst] codes={codes} sec_min={min(burst_secs)} "
        f"sec_med={sorted(burst_secs)[len(burst_secs) // 2]} sec_max={max(burst_secs)}",
        flush=True,
    )

    passed, reasons = evaluate_gate(
        long_results, burst_codes, min_json_ok, args.min_burst_success, args.max_sec
    )
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "judge_base_url": base_url,
        "long_runs": args.long_runs,
        "min_json_ok": min_json_ok,
        "burst": args.burst,
        "long_results": long_results,
        "burst_codes": codes,
        "burst_secs": burst_secs,
        "passed": passed,
        "reasons": reasons,
    }
    out = Path(args.json_out) if args.json_out else DEFAULT_OUT / (
        re.sub(r"[^A-Za-z0-9._-]", "_", model)
        + "_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {out}", flush=True)
    if passed:
        print("[verdict] PASS 可以换裁判", flush=True)
        return 0
    for reason in reasons:
        print("[verdict] FAIL " + reason, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
