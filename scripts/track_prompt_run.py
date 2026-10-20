#!/usr/bin/env python
"""W3 D14 · 一次 prompt 对比 = 一个 MLflow run（作品集侧实验追踪入口）

用法：
    # 0) 先探路：不碰 MLflow，只跑评估并打印指标（server 没起也能验证链路）
    python scripts/track_prompt_run.py --prompt-version v0 --dry-run

    # 1) 正式记录：一个 prompt 版本 = 一个 run
    python scripts/track_prompt_run.py --prompt-version v0
    python scripts/track_prompt_run.py --prompt-version v1

    # 2) 换评估集 / 换温度
    python scripts/track_prompt_run.py --prompt-version v1 --eval-set evals/smoke_set.jsonl
    python scripts/track_prompt_run.py --prompt-version v1 --temperature 0.7

四条设计取舍（都是踩过坑才定的）：
  1) 指标必须带「评估集版本」（tag: eval_set_version）。
     换了一套题，p95 和拒答率根本不可比 —— 没有这个 tag，两个月后你分不清
     「指标变好」是 prompt 改好了，还是题变简单了。
  2) 先 warm-up 一发，且不计入指标。
     实测：本机网关首包 29.3s（冷启动），第二发起 2.1s。不预热的话 n=10 的
     p95 直接等于那个 29s，整组数字报废。
  3) OBSERVABILITY 默认强制 off。
     .env 里是 langfuse；今天比的是「模型行为」，不是观测链路。Langfuse 上报虽异步，
     但队列/重试的抖动会混进延迟分布。要开就显式 TRACK_OBSERVABILITY=langfuse。
  4) 直接调 app.llm.chat()，不绕 /chat HTTP。
     少一层序列化与网络，指标只反映模型；也省得先起服务。D16 接 prompt 版本时
     再改成走服务（那时才需要验证响应头 X-Prompt-Version）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

# ⚠️ 必须在 import observability 之前定死（它在导入时读这个 env）
os.environ["OBSERVABILITY"] = os.getenv("TRACK_OBSERVABILITY", "off").strip().lower() or "off"

import llm  # noqa: E402
import observability  # noqa: E402

# ---------------------------------------------------------------------------
# prompt 版本表 —— D14 先内联，D16 抽成 prompts/structured_json/v1.yaml
# ---------------------------------------------------------------------------
PROMPTS: dict[str, str] = {
    # v0 = 线上现状（app/llm.py 的 STRUCTURED_SYSTEM_PROMPT，一字不改）
    "v0": (
        "你是一个只输出 JSON 的助手。始终用一个合法的 JSON object 作答，不要输出任何额外文字。"
    ),
    # v1 = 加领域约束 + 字段约定 + 明确拒答口径
    "v1": (
        "你是一个资金与财务领域的问答助手，只输出 JSON。\n"
        "回答必须是一个 JSON object，包含三个字段："
        "answer（字符串，回答正文）、citations（字符串数组，依据来源，可为空）、"
        "confidence（0 到 1 的小数）。\n"
        "若问题超出资金/财务领域，或你无法确定答案，"
        'answer 必须严格等于「无法确定」，不要编造。'
    ),
}

# 拒答判据：命中任一即算一次拒答。
# ⚠️ 这是**启发式**，不是真评估 —— 只能抓「明确拒答」，抓不到「一本正经地胡编」。
#    W6 换 RAGAS 才是正解，今天先用它当廉价代理指标。
REFUSAL_MARKERS = ("无法确定", "不在此范围", "无法回答", "无法从", "不知道")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def git_sha() -> tuple[str, bool]:
    """(短 sha, 是否有未提交改动)。快照五件套里的「代码版本」。"""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        ).stdout.strip())
        return sha or "unknown", dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", True


def percentile(values: list[float], p: float) -> float:
    """最近秩法（nearest-rank）。n=10 时 p95 取最大值 —— 这是样本量决定的事实，别当成 bug。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def load_eval_set(path: Path) -> list[dict[str, Any]]:
    items = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def call_once(question: str, system: str, temperature: float, max_tokens: int) -> dict[str, Any]:
    """打一发，返回逐条记录（同时也是 outputs.jsonl 的一行）。"""
    t0 = time.perf_counter()
    error = None
    out = None
    try:
        out = llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            structured=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - 上游/配置错误都要落进记录，不能让整批中断
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - t0) * 1000

    if out is None:
        return {
            "question": question, "latency_ms": round(latency_ms, 1), "error": error,
            "format_ok": False, "refused": False, "answer": "",
        }

    answer = out.content if isinstance(out.content, str) else json.dumps(out.content, ensure_ascii=False)
    return {
        "question": question,
        "latency_ms": round(latency_ms, 1),
        "format_ok": not out.parse_failed,          # JSON 解析成功 = 格式合规
        "refused": any(m in answer for m in REFUSAL_MARKERS),
        "answer": answer,
        "usage": out.usage,
        "attempts": out.attempts,
        "degraded": out.degraded,
        "error": error,
    }


def summarize(records: list[dict], model: str) -> dict[str, Any]:
    """只算卡片要求的五个指标。

    ⚠️ 刻意**不算**「关键词命中率」：v1 的 answer 措辞不固定（同一事实有五种说法），
       实测两条真样本都命中不了，指标噪声比信号大。质量问题留给 W6 的 RAGAS
       （faithfulness / answer relevancy 那套），今天先只量「能不能稳定产出 + 快不快 + 贵不贵」。
    """
    ok = [r for r in records if not r.get("error")]
    latencies = [r["latency_ms"] for r in ok]
    costs = []
    for r in ok:
        cost = observability.estimate_cost(model, r.get("usage"))["total"]
        if cost is not None:
            costs.append(cost)

    return {
        "n_items": len(records),
        "n_ok": len(ok),
        "error_rate": round(1 - len(ok) / len(records), 4) if records else 0.0,
        "p50_latency_ms": round(percentile(latencies, 50), 1),
        "p95_latency_ms": round(percentile(latencies, 95), 1),
        "avg_cost_usd": round(sum(costs) / len(costs), 8) if costs else None,
        "format_ok_rate": round(sum(1 for r in ok if r["format_ok"]) / len(ok), 4) if ok else 0.0,
        "refusal_rate": round(sum(1 for r in ok if r["refused"]) / len(ok), 4) if ok else 0.0,
    }


def write_summary(path: Path, metrics: dict, params: dict, records: list[dict]) -> None:
    lines = ["# prompt 对比实验摘要", "", "## 参数", ""]
    lines += [f"- `{k}`: {v}" for k, v in params.items()]
    lines += ["", "## 指标", "", "| 指标 | 值 |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in metrics.items()]
    lines += ["", "## 逐条结果", "", "| # | 延迟ms | 格式 | 拒答 | 问题 |", "| --- | --- | --- | --- | --- |"]
    for i, r in enumerate(records, 1):
        lines.append(
            f"| {i} | {r.get('latency_ms')} | {'OK' if r.get('format_ok') else 'FAIL'} | "
            f"{'YES' if r.get('refused') else '-'} | {r.get('question', '')[:40]} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-version", default="v0", choices=sorted(PROMPTS))
    ap.add_argument("--eval-set", default="evals/smoke_set.jsonl")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量），调试用")
    ap.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    ap.add_argument("--experiment", default="qa-prompt-eval")
    ap.add_argument("--tag", action="append", default=[], help="附加 tag，形如 k=v，可重复")
    ap.add_argument("--dry-run", action="store_true", help="只跑评估并打印，不写 MLflow")
    args = ap.parse_args()

    system = PROMPTS[args.prompt_version]
    prompt_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()[:8]

    items = load_eval_set(REPO / args.eval_set)
    if args.limit:
        items = items[: args.limit]

    sha, dirty = git_sha()
    model = llm.TIERS["primary"].model or "mock-primary"

    params = {
        "prompt_version": args.prompt_version,
        "prompt_hash": prompt_hash,
        "model": model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "llm_mode": llm.LLM_MODE,
        "git_sha": sha,
        "git_dirty": dirty,
        "eval_set": args.eval_set,
        "n_items": len(items),
    }
    tags = {
        "eval_set_version": Path(args.eval_set).stem,   # smoke_set -> 这就是「评估集版本」
        "prompt_version": args.prompt_version,
        "prompt_hash": prompt_hash,
        "llm_mode": llm.LLM_MODE,
    }
    for raw in args.tag:
        k, _, v = raw.partition("=")
        if k:
            tags[k] = v

    print(f"[info] mode={llm.LLM_MODE} model={model} prompt={args.prompt_version}({prompt_hash}) "
          f"items={len(items)} git={sha}{'-dirty' if dirty else ''}")

    # ---- warm-up：不计入指标（取舍 2）----
    t0 = time.perf_counter()
    call_once(items[0]["question"], system, args.temperature, args.max_tokens)
    print(f"[info] warm-up 完成（{time.perf_counter() - t0:.1f}s，不计入指标）")

    records: list[dict[str, Any]] = []
    for i, it in enumerate(items, 1):
        rec = call_once(it["question"], system, args.temperature, args.max_tokens)
        rec["id"] = it.get("id", str(i))
        rec["type"] = it.get("type", "knowledge")
        records.append(rec)
        flag = "ERR " if rec.get("error") else ("REF " if rec["refused"] else "ok  ")
        print(f"  [{i:02d}/{len(items)}] {flag} {rec['latency_ms']:>8.1f}ms  {it['question'][:34]}")

    metrics = summarize(records, model)

    print("\n=== 指标 ===")
    for k, v in metrics.items():
        print(f"  {k:<18} {v}")

    if args.dry_run:
        print("\n[dry-run] 未写入 MLflow")
        return 0

    import mlflow

    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_experiment(args.experiment)
    run_name = f"{args.prompt_version}-{model}-t{args.temperature}"

    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(params)
        mlflow.set_tags(tags)
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and v is not None:
                mlflow.log_metric(k, float(v))

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with (tmpdir / "outputs.jsonl").open("w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            write_summary(tmpdir / "summary.md", metrics, params, records)
            (tmpdir / "system_prompt.txt").write_text(system, encoding="utf-8")
            mlflow.log_artifacts(str(tmpdir), artifact_path="eval")

        print(f"\n[ok] run_id={run.info.run_id}")
        print(f"[ok] {args.tracking_uri}/#/experiments/ -> {args.experiment} / {run_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())