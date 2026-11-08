"""W6 D5 扩展：把 12 道应拒答 + 3 道 boundary 纳入单变量 A/B 配对。

与 w6d5_attribution.py 的关系：
- 唯一变量仍是生成端 system prompt 第 3 条（PROMPT_A -> PROMPT_B）。
- A 组逐字复用 D4 全量 responses（已生成，不重跑）；B 组用**本机网关原始通道**
  重新生成，保证与 A 组同模型同通道。
- 裁判走独立通道（Experiential /v1 + glm-5.3-flash），由启动器通过进程环境注入，
  凭据只在内存里，不落盘、不进日志。

产物全部隔离在 evals/out/w6d5_refusal/（该目录被 .gitignore 排除），
不触碰既有 w6d5 的 scores.jsonl / comparison.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import w6d5_attribution as d5  # noqa: E402
from w6d4_ragas_eval import build_evaluator, num, to_records  # noqa: E402

OUT = ROOT / "evals" / "out" / "w6d5_refusal"
B_RESPONSES = OUT / "responses_B.jsonl"
SCORES = OUT / "scores.jsonl"
SUMMARY = OUT / "summary.json"
GOLDEN = ROOT / "evals" / "golden_set.jsonl"
D4_RESPONSES = ROOT / "evals" / "out" / "w6d4" / "responses.jsonl"

KS = d5.METRIC_KEYS
REFUSAL_MARK = d5.REFUSAL
ALT_REFUSAL = "无法回答"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_env_file() -> dict[str, str]:
    """直接读 .env，不受进程环境覆盖（生成端必须保持本机网关原始配置）。"""
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def judge_env(file_env: dict[str, str]) -> dict[str, str]:
    """裁判专用 env：只覆盖 base_url 与 key，生成端 env 不受影响。"""
    base = os.environ.get("RAGAS_JUDGE_BASE_URL", "")
    key = os.environ.get("JUDGE_API_KEY", "")
    if not base or not key:
        raise SystemExit("[config] 启动器未注入 RAGAS_JUDGE_BASE_URL / JUDGE_API_KEY")
    return dict(file_env, RAGAS_JUDGE_BASE_URL=base, LLM_PRIMARY_API_KEY=key)


def selected_ids() -> list[str]:
    rows = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r["id"] for r in rows if r.get("must_refuse") or r.get("type") == "boundary"]


def load_a(limit: int) -> list[dict]:
    ids = selected_ids()
    by_id: dict[str, dict] = {}
    for line in D4_RESPONSES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            by_id[obj["id"]] = obj
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"[abort] D4 产物缺题：{missing}")
    rows = [dict(by_id[i], group="A") for i in ids]
    if limit:
        rows = rows[:limit]
    models = {r.get("model") for r in rows}
    ctx = {len(r["retrieved_contexts"]) for r in rows}
    assert models == {"deepseek-v4-flash"}, f"A 组模型不一致：{models}"
    assert ctx == {5}, f"A 组上下文数不一致：{ctx}"
    assert not any(str(r["response"]).startswith("[生成失败]") for r in rows), "A 组有生成失败"
    log(f"[A] {len(rows)} 题 | 模型 {models} | 每题上下文 {ctx}")
    return rows


def load_b_done() -> dict[str, dict]:
    if not B_RESPONSES.exists():
        return {}
    out: dict[str, dict] = {}
    for line in B_RESPONSES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            if not str(o.get("response", "")).startswith("[生成失败]"):
                out[o["id"]] = o
    return out


def generate_b(a_rows: list[dict], env: dict[str, str]) -> list[dict]:
    from openai import OpenAI

    done = load_b_done()
    todo = [r for r in a_rows if r["id"] not in done]
    log(f"[B] 目标 {len(a_rows)} 题 | 已有 {len(a_rows) - len(todo)} | 待生成 {len(todo)}")
    if todo:
        client = OpenAI(
            base_url=env["LLM_PRIMARY_BASE_URL"],
            api_key=env["LLM_PRIMARY_API_KEY"],
            timeout=180.0,
            max_retries=0,
        )
        model = env["LLM_PRIMARY_MODEL"]
        assert model == "deepseek-v4-flash", f"生成模型必须与 A 组一致，当前 {model}"
        t0 = time.time()
        for n, r in enumerate(todo, 1):
            text, latency = d5.call_llm(
                client, model, r["user_input"], list(r["retrieved_contexts"]), d5.PROMPT_B
            )
            item = {
                "id": r["id"],
                "user_input": r["user_input"],
                "response": text,
                "retrieved_contexts": list(r["retrieved_contexts"]),
                "reference": r["reference"],
                "must_refuse": bool(r.get("must_refuse")),
                "type": r.get("type"),
                "source_doc": r.get("source_doc"),
                "model": model,
                "prompt": "B",
                "latency_s": latency,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            }
            done[r["id"]] = item
            with B_RESPONSES.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            log(f"[gen] {n}/{len(todo)} {r['id']} {latency}s | {len(text)} 字")
        log(f"[B] 生成完成，耗时 {time.time() - t0:.0f}s")
    return [dict(done[r["id"]], group="B") for r in a_rows if r["id"] in done]


def load_scores() -> dict[tuple[str, str], dict]:
    if not SCORES.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    for line in SCORES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            out[(o["group"], o["id"])] = o
    return out


def save_scores(scores: dict[tuple[str, str], dict]) -> None:
    with SCORES.open("w", encoding="utf-8") as fh:
        for key in sorted(scores):
            fh.write(json.dumps(scores[key], ensure_ascii=False) + "\n")


def score(groups: dict[str, list[dict]], jenv: dict[str, str], args) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate

    scores = load_scores()
    need_any = any(
        scores.get((g, r["id"]), {}).get(k) is None
        for g in ("A", "B")
        for r in groups[g]
        for k in KS
    )
    if not need_any:
        log("[score] 四项已齐全，跳过评分")
        return scores

    llm, emb, metrics, run_config = build_evaluator(
        jenv, args.workers, args.retries, args.max_wait, args.judge
    )
    log(f"[score] 裁判 {args.judge} | 并发 {args.workers} | 批大小 {args.batch_size}")
    t_start = time.time()
    for metric in metrics:
        name = metric.name
        need = [
            (g, r)
            for g in ("A", "B")
            for r in groups[g]
            if scores.get((g, r["id"]), {}).get(name) is None
        ]
        if not need:
            continue
        need.sort(key=lambda x: (x[1]["id"], x[0]))
        for start in range(0, len(need), args.batch_size):
            batch = need[start : start + args.batch_size]
            samples = [
                SingleTurnSample(
                    user_input=r["user_input"],
                    response=r["response"],
                    retrieved_contexts=[str(c) for c in r["retrieved_contexts"]],
                    reference=str(r["reference"]),
                )
                for _, r in batch
            ]
            t0 = time.time()
            try:
                result = evaluate(
                    EvaluationDataset(samples=samples),
                    metrics=[metric],
                    llm=llm,
                    embeddings=emb,
                    run_config=run_config,
                    raise_exceptions=False,
                    show_progress=False,
                )
                records = to_records(result)
            except Exception as exc:
                log(f"[batch-fail] {name}: {type(exc).__name__}: {str(exc)[:140]}")
                records = [{} for _ in batch]
            for (g, r), rec in zip(batch, records, strict=False):
                key = (g, r["id"])
                item = dict(scores.get(key, {}))
                item.update(
                    {
                        "group": g,
                        "id": r["id"],
                        "type": r.get("type"),
                        "must_refuse": bool(r.get("must_refuse")),
                        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                        name: num(rec.get(name)) if isinstance(rec, dict) else None,
                    }
                )
                scores[key] = item
            save_scores(scores)
            valid = sum(1 for g, r in batch if scores[(g, r["id"])].get(name) is not None)
            log(
                f"[batch] {name} {start + len(batch)}/{len(need)} | 出分 {valid}/{len(batch)} "
                f"| {time.time() - t0:.0f}s | 累计 {time.time() - t_start:.0f}s"
            )
            if args.batch_wait and start + args.batch_size < len(need):
                time.sleep(args.batch_wait)
    return scores


def is_refusal(text: str) -> bool:
    return REFUSAL_MARK in text or ALT_REFUSAL in text


def behavior(groups: dict[str, list[dict]]) -> dict:
    """确定性行为分：拒答率、误拒答、回答形态。拒答题不与可答题混进同一均值。"""
    a_by = {r["id"]: r for r in groups["A"]}
    b_by = {r["id"]: r for r in groups["B"]}
    per = []
    for qid in sorted(a_by):
        a, b = a_by[qid], b_by[qid]
        per.append(
            {
                "id": qid,
                "type": a.get("type"),
                "must_refuse": bool(a.get("must_refuse")),
                "A_refused": is_refusal(a["response"]),
                "B_refused": is_refusal(b["response"]),
                "A_chars": len(a["response"]),
                "B_chars": len(b["response"]),
                "A_meta": bool(d5.META_PATTERN.search(a["response"])),
                "B_meta": bool(d5.META_PATTERN.search(b["response"])),
            }
        )
    out: dict = {"per_question": per}
    for label, pred in (
        ("refuse", lambda p: p["must_refuse"]),
        ("boundary", lambda p: p["type"] == "boundary"),
    ):
        rows = [p for p in per if pred(p)]
        out[label] = {
            "n": len(rows),
            "A_refused": sum(p["A_refused"] for p in rows),
            "B_refused": sum(p["B_refused"] for p in rows),
            "flips": [p["id"] for p in rows if p["A_refused"] != p["B_refused"]],
            "not_refused_A": [p["id"] for p in rows if not p["A_refused"]],
            "not_refused_B": [p["id"] for p in rows if not p["B_refused"]],
            "A_chars_mean": round(sum(p["A_chars"] for p in rows) / len(rows), 1) if rows else None,
            "B_chars_mean": round(sum(p["B_chars"] for p in rows) / len(rows), 1) if rows else None,
            "A_meta_hits": sum(p["A_meta"] for p in rows),
            "B_meta_hits": sum(p["B_meta"] for p in rows),
        }
    return out


def build_summary(groups: dict[str, list[dict]], scores: dict, behav: dict, args) -> dict:
    per = []
    for r in groups["A"]:
        sa = scores.get(("A", r["id"]), {})
        sb = scores.get(("B", r["id"]), {})
        row = {
            "id": r["id"],
            "type": r.get("type"),
            "must_refuse": bool(r.get("must_refuse")),
        }
        for k in KS:
            row[f"{k}_A"] = sa.get(k)
            row[f"{k}_B"] = sb.get(k)
            row[f"{k}_delta"] = (
                round(sb[k] - sa[k], 4) if sa.get(k) is not None and sb.get(k) is not None else None
            )
        per.append(row)
    by_group = {}
    for label in ("refuse", "boundary"):
        if label == "refuse":
            rows = [p for p in per if p["must_refuse"]]
        else:
            rows = [p for p in per if p["type"] == "boundary"]
        metrics = {}
        for k in KS:
            # 只用 A/B 都有分的配对题算均值；有一侧缺分时若各算各的，
            # mean_A / mean_B / delta 会落在不同题集上，不能直接相减。
            paired = [
                p for p in rows
                if p[f"{k}_A"] is not None and p[f"{k}_B"] is not None
            ]
            a = [p[f"{k}_A"] for p in paired]
            b = [p[f"{k}_B"] for p in paired]
            ds = [p[f"{k}_delta"] for p in paired if p[f"{k}_delta"] is not None]
            metrics[k] = {
                "mean_A": round(sum(a) / len(a), 4) if a else None,
                "mean_B": round(sum(b) / len(b), 4) if b else None,
                "delta": round(sum(ds) / len(ds), 4) if ds else None,
                "n_paired": len(ds),
            }
        by_group[label] = {"n": len(rows), "metrics": metrics, "behavior": behav[label]}
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "judge": args.judge,
        "generate_model": groups["A"][0].get("model"),
        "generate_channel": "本机网关 .env 原始 LLM_PRIMARY_BASE_URL（与 A 组一致）",
        "judge_channel": "独立通道，启动器进程环境注入",
        "only_variable": d5.ONLY_VARIABLE,
        "controlled": {
            "questions": "golden set 中 must_refuse 或 type=boundary 的 15 题",
            "A": "逐字复用 evals/out/w6d4/responses.jsonl（含 5 个检索上下文）",
            "B": "同模型同通道、逐字复用 A 组上下文，只换 system prompt 第 3 条",
            "reference": "evals/golden_set.jsonl 同一份；拒答题写的是期望行为而非事实答案",
        },
        "by_group": by_group,
        "behavior": behav,
        "per_question": per,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default="glm-5.3-flash")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（试水用，0 = 全部 15 题）")
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch-wait", type=int, default=5)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-wait", type=int, default=60)
    ap.add_argument("--gen-only", action="store_true", help="只生成 B 组，不评分")
    ap.add_argument("--report-only", action="store_true", help="不生成不评分，只重建汇总")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    file_env = read_env_file()
    a_rows = load_a(args.limit)
    if args.report_only:
        done_b = load_b_done()
        b_rows = [dict(done_b[r["id"]], group="B") for r in a_rows if r["id"] in done_b]
    else:
        b_rows = generate_b(a_rows, file_env)
    if len(b_rows) != len(a_rows):
        log(f"[abort] B 组 {len(b_rows)} 题 != A 组 {len(a_rows)} 题，先补齐再汇总")
        return 2
    groups = {"A": a_rows, "B": b_rows}

    if args.gen_only:
        log("[done] 生成阶段完成，未评分")
        return 0

    if args.report_only:
        scores = load_scores()
    else:
        scores = score(groups, judge_env(file_env), args)
    behav = behavior(groups)
    summary = build_summary(groups, scores, behav, args)
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[summary] -> {SUMMARY}")

    missing = [
        (g, r["id"], k)
        for g in ("A", "B")
        for r in groups[g]
        for k in KS
        if scores.get((g, r["id"]), {}).get(k) is None
    ]
    if missing:
        log(f"[incomplete] 缺 {len(missing)} 个分数：{missing[:8]}")
        return 2
    log("[done] 四项全齐、NaN 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
