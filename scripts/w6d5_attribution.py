# -*- coding: utf-8 -*-
"""W6 D5 动手实践：指标归因的单变量 A/B 实验。

任务卡 11/13 要求「针对最低的那个指标做一次改进实验，观察数字怎么动」，卡点是
「一次只改一个变量」。D4 小样里最低的是 answer_relevancy(0.5817)，按「指标 → 该改哪一层」
对照表它归生成层，所以本次只改生成端 system prompt 的第 3 条输出格式约束。

用法（在作品集仓根目录）:
    ./.venv-eval/Scripts/python.exe scripts/w6d5_attribution.py --limit 10
    ./.venv-eval/Scripts/python.exe scripts/w6d5_attribution.py --report-only
    ./.venv-eval/Scripts/python.exe scripts/w6d5_attribution.py --force-generate

设计要点:
    - A 组 = D4 已产出的 g001–g010 回答（原 prompt），B 组 = 同题同检索上下文、只换 prompt 重新生成。
      检索上下文逐字复用 A 组，不重切语料、不重跑 RRF，保证检索层零改动。
    - 两组的四项指标在同一个 evaluate() 批次里交错评分，裁判用固定别名（不用会漂移的 auto）。
    - context_recall 与 context_precision 不读 response，只依赖 retrieved_contexts + reference，
      A/B 真实值本该完全相同 → 这两项的差值天然是「裁判噪声标尺」，用来判断主指标变化是否超出噪声。
    - 另算一个完全不吃 LLM 的确定性对照：本地 bge 直接算 question↔response 余弦。

产出（evals/out/ 被 gitignore）:
    evals/out/w6d5/responses_B.jsonl   B 组回答
    evals/out/w6d5/scores.jsonl        A/B 逐题四项分数
    evals/out/w6d5/comparison.json     前后对比、控制变量清单、噪声标尺、逐题明细
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = ROOT / "evals" / "out" / "w6d5"
D4_RESPONSES = ROOT / "evals" / "out" / "w6d4" / "responses.jsonl"
B_RESPONSES = OUT / "responses_B.jsonl"
SCORES = OUT / "scores.jsonl"
COMPARISON = OUT / "comparison.json"

# 与 scripts/w6d4_rag_chain.py 的 SYSTEM_PROMPT 逐字一致。
PROMPT_A = """你是一个只依据给定资料回答问题的助手。必须遵守以下规则：
1. 只能用「资料」里的内容作答，不要补充资料之外的数字、命令、结论或常识。
2. 资料里没有能回答这个问题的信息时，只回复一句：资料中没有覆盖这个问题，无法回答。不要猜，不要用通用知识补齐。
3. 回答控制在 2 到 4 句，直接给结论和依据；不要复述问题，不要加「根据资料」这类前缀。
4. 资料里的命令行、表格行、章节标题可以原样引用。
"""

# 唯一变量：只改第 3 条。第 1、2、4 条与 PROMPT_A 逐字相同。
PROMPT_B = """你是一个只依据给定资料回答问题的助手。必须遵守以下规则：
1. 只能用「资料」里的内容作答，不要补充资料之外的数字、命令、结论或常识。
2. 资料里没有能回答这个问题的信息时，只回复一句：资料中没有覆盖这个问题，无法回答。不要猜，不要用通用知识补齐。
3. 回答只用 1 到 2 句：第一句直接给结论，并带上问题里问的那个对象或术语；第二句给依据。不要复述整句问题，不要加「根据资料」「块 1」这类前缀或出处说明，不要写总结、提示语或客套话。
4. 资料里的命令行、表格行、章节标题可以原样引用。
"""

METRIC_KEYS = (
    "faithfulness",
    "answer_relevancy",
    "context_recall",
    "llm_context_precision_with_reference",
)
# 这两项不读 response，A/B 真实值应当相等，差值即裁判噪声。
NOISE_KEYS = ("context_recall", "llm_context_precision_with_reference")

CONTROLLED = {
    "questions": "golden set g001–g010（与 D4 小样同一批）",
    "corpus": "evals/corpus/ 六篇冻结快照，递归 B(400/50) 切 330 块",
    "retrieval": "bge-small-zh-v1.5 余弦 + jieba BM25，RRF(k=60)，top-5",
    "retrieved_contexts": "B 组逐字复用 A 组每题的 5 个块，不重切、不重排",
    "reference": "evals/golden_set.jsonl 同一份标准答案",
    "generate_model": "deepseek-v4-flash（A/B 相同）",
    "generate_params": {"temperature": 0, "max_tokens": 400},
    "judge_model": "固定别名，A/B 交错放进同一 evaluate() 批次",
    "embedding": "models/bge-small-zh-v1.5（512 维，L2 归一化）",
    "metrics_api": "ragas 0.4.3 legacy metrics",
}
ONLY_VARIABLE = (
    "生成端 system prompt 第 3 条（输出格式约束）："
    "「2–4 句、直接给结论和依据、不要复述问题」→「1–2 句、首句带问题关键对象、"
    "第二句给依据、显式禁元信息与客套」。第 1、2、4 条逐字不变。"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> dict[str, str]:
    import os  # 环境变量优先：换独立裁判时注入 judge 通道，不必改 .env 或落盘凭据

    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip()
        env[name] = os.environ.get(name, value.strip().strip('"').strip("'"))
    return env


def load_a(limit: int) -> list[dict]:
    by_id: dict[str, dict] = {}
    for line in D4_RESPONSES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if str(obj.get("response", "")).startswith("[生成失败]"):
            continue
        if not obj.get("retrieved_contexts"):
            continue
        by_id[obj["id"]] = obj
    ids = sorted(by_id)[:limit]
    models = {by_id[i].get("model") for i in ids}
    ctx_counts = {len(by_id[i]["retrieved_contexts"]) for i in ids}
    log(f"[A] {len(ids)} 题 | 生成模型 {models} | 每题上下文数 {ctx_counts}")
    assert len(models) == 1 and ctx_counts == {5}, "A 组数据不一致，先修再跑"
    return [dict(by_id[i], group="A") for i in ids]


def load_b() -> dict[str, dict]:
    if not B_RESPONSES.exists():
        return {}
    out: dict[str, dict] = {}
    for line in B_RESPONSES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if str(obj.get("response", "")).startswith("[生成失败]"):
            continue
        out[obj["id"]] = obj
    return out


def call_llm(client, model: str, question: str, contexts: list[str], prompt: str) -> tuple[str, float]:
    """与 w6d4_rag_chain.call_llm 参数逐项对齐，只有 system prompt 不同。"""
    blocks = "\n\n".join(f"[块 {i + 1}]\n{c}" for i, c in enumerate(contexts))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"问题：{question}\n\n资料：\n{blocks}"},
        ],
        "temperature": 0,
        "max_tokens": 400,
    }
    delay = 5.0
    last: Exception | None = None
    for attempt in range(6):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**payload)
            text = (resp.choices[0].message.content or "").strip()
            return text, round(time.time() - t0, 2)
        except Exception as exc:
            last = exc
            log(f"[retry {attempt + 1}/6] {type(exc).__name__}: {str(exc)[:110]} → sleep {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    return f"[生成失败] {type(last).__name__}: {str(last)[:160]}", 0.0


def generate_b(a_rows: list[dict], env: dict[str, str], force: bool) -> list[dict]:
    from openai import OpenAI

    done = {} if force else load_b()
    todo = [r for r in a_rows if r["id"] not in done]
    log(f"[B] 目标 {len(a_rows)} 题 | 已有 {len(a_rows) - len(todo)} | 本次待生成 {len(todo)}")
    if not todo:
        return [dict(done[r["id"]], group="B") for r in a_rows]

    client = OpenAI(
        base_url=env["RAGAS_JUDGE_BASE_URL"],
        api_key=env["LLM_PRIMARY_API_KEY"],
        timeout=180.0,
        max_retries=0,
    )
    model = env["RAGAS_JUDGE_MODEL"]
    assert model == "deepseek-v4-flash", f"生成模型必须与 A 组一致，当前 {model}"
    t0 = time.time()
    for n, r in enumerate(todo, 1):
        text, latency = call_llm(
            client, model, r["user_input"], list(r["retrieved_contexts"]), PROMPT_B
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
    failed = [r["id"] for r in a_rows if r["id"] not in done]
    if failed:
        log(f"[B] 仍有失败题：{failed}")
    return [dict(done[r["id"]], group="B") for r in a_rows if r["id"] in done]


def load_scores() -> dict[tuple[str, str], dict]:
    if not SCORES.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    for line in SCORES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            out[(obj["group"], obj["id"])] = obj
    return out


def save_scores(scores: dict[tuple[str, str], dict]) -> None:
    lines = [json.dumps(scores[k], ensure_ascii=False) for k in sorted(scores)]
    SCORES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 6)


def score(groups: dict[str, list[dict]], env: dict[str, str], args) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate

    from w6d4_ragas_eval import build_evaluator, to_records

    scores = load_scores()
    need_any = any(
        scores.get((g, r["id"]), {}).get(k) is None
        for g in ("A", "B")
        for r in groups[g]
        for k in METRIC_KEYS
    )
    if not need_any:
        log("[score] 四项已齐全，跳过评分")
        return scores

    llm, emb, metrics, run_config = build_evaluator(
        env, args.workers, args.retries, args.max_wait, args.judge
    )
    if args.metrics:
        wanted = {x.strip() for x in args.metrics.split(",") if x.strip()}
        metrics = [m for m in metrics if m.name in wanted]
        log(f"[score] 只跑指标 {[m.name for m in metrics]}")
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
        # A/B 按题号交错，保证同一次 evaluate 里两组挨着，摊平裁判漂移。
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
                log(f"[batch-fail] {name} {batch[0][1]['id']}..{batch[-1][1]['id']}: "
                    f"{type(exc).__name__}: {str(exc)[:160]}")
                records = [{} for _ in batch]
            for (g, r), rec in zip(batch, records):
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
            valid = sum(1 for (g, r) in batch if scores[(g, r["id"])].get(name) is not None)
            log(f"[batch] {name} {start + len(batch)}/{len(need)} | 本批出分 {valid}/{len(batch)} "
                f"| {time.time() - t0:.0f}s | 累计 {time.time() - t_start:.0f}s")
            if args.batch_wait and start + args.batch_size < len(need):
                time.sleep(args.batch_wait)
    return scores


def deterministic_cosine(groups: dict[str, list[dict]]) -> dict:
    """本地 bge 直接算 question↔response 余弦，不经 LLM，作为可复现的交叉验证。"""
    import numpy as np
    from langchain_community.embeddings import HuggingFaceEmbeddings

    hf = HuggingFaceEmbeddings(
        model_name=str(ROOT / "models" / "bge-small-zh-v1.5"),
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )
    out: dict[str, dict] = {}
    for g in ("A", "B"):
        vals: list[float] = []
        for r in groups[g]:
            q, resp = hf.embed_documents([r["user_input"], r["response"]])
            vals.append(round(float(np.dot(q, resp)), 4))
        out[g] = {
            "mean": round(sum(vals) / len(vals), 4) if vals else None,
            "per_question": {r["id"]: v for r, v in zip(groups[g], vals)},
        }
    if out["A"]["mean"] is not None and out["B"]["mean"] is not None:
        out["delta"] = round(out["B"]["mean"] - out["A"]["mean"], 4)
    return out


META_PATTERN = re.compile(r"根据资料|依据资料|资料中(没有|未)|块\s*\d|如上所述|综上|以上")
REFUSAL = "资料中没有覆盖这个问题"


def response_shape(groups: dict[str, list[dict]]) -> dict:
    """回答形态：证明唯一变量确实生效，且不依赖任何 LLM 裁判。"""
    out: dict[str, dict] = {}
    for g in ("A", "B"):
        lengths = [len(r["response"]) for r in groups[g]]
        sentences = [len(re.findall(r"[。！？]", r["response"])) for r in groups[g]]
        out[g] = {
            "avg_chars": round(statistics.mean(lengths), 1),
            "avg_sentences": round(statistics.mean(sentences), 2),
            "with_meta_info": sum(bool(META_PATTERN.search(r["response"])) for r in groups[g]),
            "refusal_phrase": sum(REFUSAL in r["response"] for r in groups[g]),
            "per_question_chars": {r["id"]: n for r, n in zip(groups[g], lengths)},
        }
    out["avg_chars_delta"] = round(out["B"]["avg_chars"] - out["A"]["avg_chars"], 1)
    return out


def dispersion(groups, scores) -> dict:
    """逐题 Δ 的离散度与裁判噪声明细。均值很小不等于没变化，要看波动盖不盖得住它。"""
    ids = [r["id"] for r in groups["A"]]
    out: dict[str, dict] = {}
    for k in METRIC_KEYS:
        vals = [
            scores[("B", i)][k] - scores[("A", i)][k]
            for i in ids
            if scores.get(("A", i), {}).get(k) is not None
            and scores.get(("B", i), {}).get(k) is not None
        ]
        if not vals:
            continue
        out[k] = {
            "n": len(vals),
            "mean_delta": round(statistics.mean(vals), 4),
            "stdev": round(statistics.pstdev(vals), 4),
            "abs_mean": round(statistics.mean([abs(v) for v in vals]), 4),
            "up": sum(v > 0 for v in vals),
            "down": sum(v < 0 for v in vals),
            "flat": sum(v == 0 for v in vals),
        }
    return out


def build_comparison(groups, scores, cosine, shape, args) -> dict:
    summary: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "only_changed_variable": ONLY_VARIABLE,
        "controlled_variables": CONTROLLED,
        "prompts": {"A": PROMPT_A, "B": PROMPT_B},
        "judge_model": args.judge,
        "question_count": len(groups["A"]),
        "averages": {},
        # 只用 A/B 同题都有分的配对样本算差值，避免某组少一题导致均值口径不等。
        "paired": {},
        "noise_baseline": {},
        "per_question": [],
        "deterministic_cosine": cosine,
        "response_shape": shape,
        "dispersion": dispersion(groups, scores),
    }
    for g in ("A", "B"):
        picked = [scores.get((g, r["id"]), {}) for r in groups[g]]
        summary["averages"][g] = {}
        for k in METRIC_KEYS:
            vals = [x[k] for x in picked if x.get(k) is not None]
            summary["averages"][g][k] = {
                "mean": round(sum(vals) / len(vals), 4) if vals else None,
                "valid": len(vals),
                "nan": len(picked) - len(vals),
            }
    for k in METRIC_KEYS:
        pairs = [
            (scores[("A", r["id"])][k], scores[("B", r["id"])][k])
            for r in groups["A"]
            if scores.get(("A", r["id"]), {}).get(k) is not None
            and scores.get(("B", r["id"]), {}).get(k) is not None
        ]
        summary["paired"][k] = {
            "n": len(pairs),
            "mean_A": round(sum(a for a, _ in pairs) / len(pairs), 4) if pairs else None,
            "mean_B": round(sum(b for _, b in pairs) / len(pairs), 4) if pairs else None,
            "delta": round(sum(b - a for a, b in pairs) / len(pairs), 4) if pairs else None,
        }
    # 这两项不读 response，真实值 A=B，配对差值全部来自裁判随机性。
    summary["noise_baseline"] = {
        k: summary["paired"][k]["delta"] for k in NOISE_KEYS
    }
    for r in groups["A"]:
        a = scores.get(("A", r["id"]), {})
        b = scores.get(("B", r["id"]), {})
        row = {"id": r["id"], "type": r.get("type")}
        for k in METRIC_KEYS:
            row[f"{k}_A"] = a.get(k)
            row[f"{k}_B"] = b.get(k)
            row[f"{k}_delta"] = (
                round(b[k] - a[k], 4)
                if a.get(k) is not None and b.get(k) is not None
                else None
            )
        summary["per_question"].append(row)
    COMPARISON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=== A（原 prompt）→ B（只改第 3 条）· 配对样本 ===")
    log(f"{'指标':<42} {'A':>8} {'B':>8} {'Δ':>9}  配对题数")
    for k in METRIC_KEYS:
        p = summary["paired"][k]
        log(f"{k:<42} {str(p['mean_A']):>8} {str(p['mean_B']):>8} "
            f"{str(p['delta']):>9}  {p['n']}")
    log("--- 全部有分题的均值（参考，口径可能不等）---")
    for k in METRIC_KEYS:
        a, b = summary["averages"]["A"][k], summary["averages"]["B"][k]
        log(f"  {k}: A {a['mean']} ({a['valid']} 题) / B {b['mean']} ({b['valid']} 题)")
    log("--- 裁判噪声标尺（这两项不该变）---")
    for k in NOISE_KEYS:
        log(f"  {k}: Δ = {summary['noise_baseline'][k]}")
    log(f"--- 确定性余弦 question↔response: A {cosine['A']['mean']} "
        f"→ B {cosine['B']['mean']} (Δ {cosine.get('delta')}) ---")
    log("--- 回答形态（唯一变量确实生效）---")
    for k in ("avg_chars", "avg_sentences", "with_meta_info", "refusal_phrase"):
        log(f"  {k}: A {shape['A'][k]} → B {shape['B'][k]}")
    log("--- 逐题 Δ 离散度 ---")
    for k, v in summary["dispersion"].items():
        log(f"  {k}: mean {v['mean_delta']} / σ {v['stdev']} / |Δ|均值 {v['abs_mean']} "
            f"/ 涨{v['up']} 跌{v['down']} 平{v['flat']}")
    log(f"已写 {COMPARISON}")
    return summary


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    return t.strip()


def preflight(env: dict[str, str], judge: str) -> int:
    """换裁判前先体检：真实长 prompt 连发 3 次，必须能解析成 JSON。

    11-08 的教训：短 prompt 冒烟 4/4 通过，一上 ragas 密集调用就 429，
    或者模型吐思考过程 / 只回三个反引号，白白烧掉一轮 20 题。
    体检不写任何结果文件，通过才开跑。

    ⚠️ 2026-11-08 晚间废弃：这个体检对 `glm-5.3-flash` 会**假阴性**——
    本函数把 max_tokens 写死 180，且 prompt 只说「不要解释」，模型进入 thinking 后
    reasoning_tokens 吃满额度、content 为空。换裁判请改用
    `scripts/w6d4_judge_preflight.py`（长 prompt 措辞 + 12 连发 burst，退出码 0 才放行）；
    ragas 的 build_evaluator 不设 max_tokens，不受此处限制。
    """
    from openai import OpenAI

    rows = load_a(1)
    prompt = (
        "资料：\n"
        + "\n\n".join(rows[0]["retrieved_contexts"])
        + '\n\n只输出 JSON，不要解释：{"statements":["..."]}'
    )
    client = OpenAI(
        base_url=env["RAGAS_JUDGE_BASE_URL"],
        api_key=env["LLM_PRIMARY_API_KEY"],
        timeout=120,
        max_retries=0,
    )
    log(f"[preflight] 裁判 {judge} | 真实长 prompt {len(prompt)} 字 | 连发 3 次")
    good = 0
    for i in range(3):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=judge,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=180,
            )
            text = (resp.choices[0].message.content or "").strip()
            parsed = json.loads(_strip_code_fence(text))
            ok = isinstance(parsed, dict) and "statements" in parsed
            log(f"  [{i + 1}/3] {time.time() - t0:.1f}s | JSON {'OK' if ok else '缺字段'} "
                f"| {text[:60]!r}")
            good += bool(ok)
        except Exception as exc:
            log(f"  [{i + 1}/3] {time.time() - t0:.1f}s | FAIL "
                f"{type(exc).__name__}: {str(exc)[:120]}")
    # 偶发一次超时不算配置问题（ragas 内部还有 RunConfig 重试）；
    # 但 503 无 key、JSON 解析不了、或连着两次失败，都必须拦下。
    passed = good >= 2
    hint = "（有偶发超时，密集调用会更差，建议 --workers 1 并加大 --batch-wait）" if good == 2 else ""
    log(f"[preflight] {good}/3 通过 → {'可以重跑' if passed else '别开跑，先修裁判'} {hint}")
    return 0 if passed else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--judge", default="qwen3.8-27b", help="固定裁判别名，不用 auto")
    ap.add_argument("--batch-size", type=int, default=10, help="同一 evaluate() 里的样本数，A/B 交错")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch-wait", type=int, default=3)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-wait", type=int, default=20)
    ap.add_argument("--force-generate", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--skip-score", action="store_true", help="只生成 B 组回答，不评分（长跑拆阶段用）")
    ap.add_argument("--metrics", default="", help="只跑指定指标，逗号分隔；留空 = 四项全跑")
    ap.add_argument("--preflight", action="store_true",
                    help="只体检裁判：真实长 prompt 连发 3 次 + JSON 解析，不写结果")
    args = ap.parse_args()

    if args.preflight:
        return preflight(load_env(), args.judge)

    OUT.mkdir(parents=True, exist_ok=True)
    env = load_env()
    a_rows = load_a(args.limit)
    a_models = {r.get("model") for r in a_rows}
    log(f"[control] A 组生成模型 {a_models} | 唯一变量 = {ONLY_VARIABLE}")

    if args.report_only:
        done_b = load_b()
        b_rows = [dict(done_b[r["id"]], group="B") for r in a_rows if r["id"] in done_b]
    else:
        b_rows = generate_b(a_rows, env, args.force_generate)
    if len(b_rows) != len(a_rows):
        log(f"[abort] B 组 {len(b_rows)} 题 ≠ A 组 {len(a_rows)} 题，先补齐再汇总")
        return 2
    if args.skip_score:
        log("[done] 生成阶段完成，未评分；接着跑不带 --skip-score 的同一命令")
        return 0
    groups = {"A": a_rows, "B": b_rows}

    scores = load_scores()
    if not args.report_only:
        scores = score(groups, env, args)
    cosine = deterministic_cosine(groups)
    shape = response_shape(groups)
    summary = build_comparison(groups, scores, cosine, shape, args)

    missing = [
        (g, r["id"], k)
        for g in ("A", "B")
        for r in groups[g]
        for k in METRIC_KEYS
        if scores.get((g, r["id"]), {}).get(k) is None
    ]
    if missing:
        log(f"[incomplete] 缺 {len(missing)} 个分数，重跑本脚本只补缺：{missing[:8]}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
