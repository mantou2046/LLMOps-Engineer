# -*- coding: utf-8 -*-
"""W6 D4 动手实践：跑 RAGAS 四项指标，产出首份基线数字。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d4_ragas_eval.py --limit 1     # 先验证 API
    ./.venv-eval/Scripts/python.exe scripts/w6d4_ragas_eval.py --limit 10    # 10 题小样
    ./.venv-eval/Scripts/python.exe scripts/w6d4_ragas_eval.py               # 全量 50 题
    ./.venv-eval/Scripts/python.exe scripts/w6d4_ragas_eval.py --force       # 清掉旧分重跑

产出（evals/out/ 被 gitignore）:
    evals/out/w6d4/metrics.jsonl   逐题四项分数
    evals/out/w6d4/metrics.json    汇总：均值、有效题数、NaN 数、配置、耗时

口径:
    - 四项都用 ragas 0.4.3 的 legacy 指标类。collections 版要求 InstructorBaseRagasLLM，
      本次沿用 11-04 实测跑通的 LangchainLLMWrapper 路线；DeprecationWarning 记在报告里。
    - context precision 选 LLMContextPrecisionWithReference：它需要 reference，是正式口径；
      不带 reference 的 ContextUtilization 只作绕开手段，不拿来报数。
    - 裁判 = 网关 31415 的 deepseek-v4-flash；embedding = 本地 bge-small-zh-v1.5（512 维）。
    - 分批跑、逐题落盘，中断后重跑自动跳过已出分的题。429 由 RunConfig 退避，
      单题失败记 NaN，不拿其他题的分数补位。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "evals" / "out" / "w6d4"
RESPONSES = OUT_DIR / "responses.jsonl"
SCORES = OUT_DIR / "metrics.jsonl"
SUMMARY = OUT_DIR / "metrics.json"

METRIC_KEYS = (
    "faithfulness",
    "answer_relevancy",
    "context_recall",
    "llm_context_precision_with_reference",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> dict[str, str]:
    import os  # 环境变量优先：换裁判时注入进程环境即可，不必改 .env 或把凭据写进文件

    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip()
        env[name] = os.environ.get(name, value.strip().strip('"').strip("'"))
    return env


def load_responses() -> list[dict]:
    by_id: dict[str, dict] = {}
    for line in RESPONSES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # 后台进程正在写，跳过半行
        if str(obj.get("response", "")).startswith("[生成失败]"):
            continue
        if not obj.get("retrieved_contexts"):
            continue
        by_id[obj["id"]] = obj  # 同 id 取最后一条
    return [by_id[k] for k in sorted(by_id)]


def build_evaluator(
    env: dict[str, str], workers: int, retries: int, max_wait: int, judge_model: str
):
    from langchain_community.embeddings import HuggingFaceEmbeddings
    from langchain_openai import ChatOpenAI
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )
    from ragas.run_config import RunConfig

    chat = ChatOpenAI(
        base_url=env["RAGAS_JUDGE_BASE_URL"],
        api_key=env["LLM_PRIMARY_API_KEY"],
        model=judge_model,
        temperature=0,
        timeout=180,
        max_retries=2,
    )
    llm = LangchainLLMWrapper(chat)
    # embedding_factory("huggingface") 返回的对象没有 embed_query，legacy 指标会报
    # AttributeError（2026-11-06 实测）。必须再包一层 LangchainEmbeddingsWrapper。
    emb = LangchainEmbeddingsWrapper(
        HuggingFaceEmbeddings(
            model_name=str(ROOT / "models" / "bge-small-zh-v1.5"),
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    )
    metrics = [
        Faithfulness(llm=llm),
        # 网关不认 n>1，请求 3 个生成也只回 1 个；显式设 1，避免每次打无用的采样请求。
        ResponseRelevancy(llm=llm, embeddings=emb, strictness=1),
        LLMContextRecall(llm=llm),
        LLMContextPrecisionWithReference(llm=llm),
    ]
    run_config = RunConfig(
        timeout=180,
        max_retries=retries,
        max_wait=max_wait,
        max_workers=workers,
        log_tenacity=False,
    )
    return llm, emb, metrics, run_config


def to_records(result) -> list[dict]:
    try:
        return result.to_pandas().to_dict(orient="records")
    except Exception:
        return list(getattr(result, "_scores", []) or [])


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 6)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="评估前 N 题（0 = 全部可用题）")
    ap.add_argument("--batch-size", type=int, default=5, help="每批题数，便于断点与限速")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch-wait", type=int, default=15, help="批次之间等待秒数，给网关限流窗口留时间")
    ap.add_argument("--retries", type=int, default=4, help="单任务重试次数；调小可快速失败，NaN 下次重跑补")
    ap.add_argument("--max-wait", type=int, default=30, help="退避上限秒数")
    ap.add_argument("--judge-model", default="", help="裁判模型；不传则用 .env 的 RAGAS_JUDGE_MODEL")
    ap.add_argument("--force", action="store_true", help="清掉旧分数重跑（换裁判时必须）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env()
    rows = load_responses()
    if args.limit:
        rows = rows[: args.limit]
    log(f"[data] 可用回答 {len(rows)} 题")

    done: dict[str, dict] = {}
    if SCORES.exists() and not args.force:
        for line in SCORES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                obj = json.loads(line)
                done[obj["id"]] = obj  # 同 id 取最后一条
    # 只补缺的指标。上游 429 时每次 judge 调用都很贵，整题重跑会把已经算出来的分也扔掉。
    todo: list[tuple[dict, list[str]]] = []
    for r in rows:
        cur = done.get(r["id"], {})
        missing = [k for k in METRIC_KEYS if cur.get(k) is None]
        if missing:
            todo.append((r, missing))
    full = len(rows) - len(todo)
    log(f"[plan] 四项齐全 {full}/{len(rows)} | 待补 {len(todo)} 题 | 批次 {args.batch_size} | 并发 {args.workers}")
    if not todo:
        return write_summary(rows, done, env, args, 0.0)

    from ragas import SingleTurnSample, EvaluationDataset, evaluate

    judge = args.judge_model or env["RAGAS_JUDGE_MODEL"]
    log(f"[judge] {judge}")
    llm, emb, metrics, run_config = build_evaluator(
        env, args.workers, args.retries, args.max_wait, judge
    )
    t_start = time.time()

    groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row, missing in todo:
        groups[tuple(missing)].append(row)

    n_done = 0
    for need, group_rows in groups.items():
        active = [m for m in metrics if m.name in need]
        if not active:
            continue
        log(f"[group] 补 {','.join(need)}：{len(group_rows)} 题")
        for start in range(0, len(group_rows), args.batch_size):
            batch = group_rows[start : start + args.batch_size]
            samples = [
                SingleTurnSample(
                    user_input=r["user_input"],
                    response=r["response"],
                    retrieved_contexts=[str(c) for c in r["retrieved_contexts"]],
                    reference=str(r["reference"]),
                )
                for r in batch
            ]
            dataset = EvaluationDataset(samples=samples)
            t0 = time.time()
            ids = [r["id"] for r in batch]
            try:
                result = evaluate(
                    dataset,
                    metrics=active,
                    llm=llm,
                    embeddings=emb,
                    run_config=run_config,
                    raise_exceptions=False,
                    show_progress=False,
                )
                records = to_records(result)
            except Exception as exc:
                log(f"[batch-fail] {ids[0]}..{ids[-1]}: {type(exc).__name__}: {str(exc)[:180]}")
                records = [{} for _ in batch]

            for i, r in enumerate(batch):
                rec = records[i] if i < len(records) else {}
                item = dict(done.get(r["id"], {}))
                item.update(
                    {
                        "id": r["id"],
                        "type": r.get("type"),
                        "must_refuse": bool(r.get("must_refuse")),
                        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                for k in need:
                    item[k] = num(rec.get(k))
                done[item["id"]] = item
            # 整文件重写，幂等；同 id 不会出现重复行。
            SCORES.write_text(
                "\n".join(json.dumps(done[k], ensure_ascii=False) for k in sorted(done)) + "\n",
                encoding="utf-8",
            )
            n_done += len(batch)
            valid = sum(
                1 for r in batch if all(done[r["id"]].get(k) is not None for k in METRIC_KEYS)
            )
            log(f"[batch] {n_done}/{len(todo)} | 本批四项齐全 {valid}/{len(batch)} "
                f"| {time.time() - t0:.0f}s | 累计 {time.time() - t_start:.0f}s")
            if args.batch_wait:
                time.sleep(args.batch_wait)

    return write_summary(rows, done, env, args, time.time() - t_start)


def write_summary(rows, done, env, args, elapsed: float) -> int:
    picked = [done[r["id"]] for r in rows if r["id"] in done]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "questions": len(rows),
        "scored": len(picked),
        "elapsed_s": round(elapsed, 1),
        "judge_model": args.judge_model or env["RAGAS_JUDGE_MODEL"],
        "judge_base_url": env["RAGAS_JUDGE_BASE_URL"],
        "embedding": "models/bge-small-zh-v1.5 (512d)",
        "metrics_api": "ragas 0.4.3 legacy metrics (DeprecationWarning)",
        "batch_size": args.batch_size,
        "batch_wait_s": args.batch_wait,
        "max_workers": args.workers,
        "retries": args.retries,
        "max_wait_s": args.max_wait,
        "averages": {},
    }
    for k in METRIC_KEYS:
        vals = [x[k] for x in picked if x[k] is not None]
        summary["averages"][k] = {
            "mean": round(sum(vals) / len(vals), 4) if vals else None,
            "valid": len(vals),
            "nan": len(picked) - len(vals),
        }
    by_type: dict[str, dict] = {}
    for x in picked:
        g = by_type.setdefault(str(x["type"]), {k: [] for k in METRIC_KEYS})
        for k in METRIC_KEYS:
            if x[k] is not None:
                g[k].append(x[k])
    summary["by_type"] = {
        t: {k: (round(sum(v) / len(v), 4) if v else None) for k, v in g.items()}
        for t, g in by_type.items()
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=== 四项基线 ===")
    for k, v in summary["averages"].items():
        log(f"  {k:<40} mean={v['mean']}  有分 {v['valid']}/{len(picked)}  NaN {v['nan']}")
    log(f"已写 {SUMMARY}")
    nan_total = sum(v["nan"] for v in summary["averages"].values())
    return 0 if picked and nan_total == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
