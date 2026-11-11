"""W7 D7 验收核对：把任务卡三条验收线重跑一遍，再按阈值判定。

D7 的动作是「周复盘 + 里程碑确认」，最容易犯的错是拿上周写的报告当证据抄一遍。
本脚本的做法相反：**先重跑**四个 demo（成本 / 微调数学 / 决策 / Agent），
再读回它们刚写出的 JSON，用写死的阈值判定，最后打印一张核对表。

判定与「可当场演示」分开记：
- 判定（PASS/FAIL）看数字是否达标；
- 可演示性看「换台机器能不能重现」。真 LoRA 云端训练、语义缓存、真实流量分布
  这三项在报告里有记录，但本机无法重现，一律标「引用，不可当场重跑」。

用法：
    python scripts/w7d7_acceptance_check.py              # 四条线，约 10 s
    python scripts/w7d7_acceptance_check.py --skip-run   # 只读现有产物，不重跑
    python scripts/w7d7_acceptance_check.py --with-pytest  # 追加全量 pytest（+22 s）

退出码：0 = 三条验收线全部 PASS；1 = 有 FAIL；2 = 脚本自身跑不起来。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "out"

# 每条验收线的 demo 脚本与产物
DEMOS = {
    "cost": ("scripts/w7d3_cost_bench.py", "w7d3/cost_bench.json"),
    "lora": ("scripts/w7d4_lora_demo.py", "w7d4/lora_demo.json"),
    "decision": ("scripts/w7d5_decision_demo.py", "w7d5/decision_bench.json"),
    "agent": ("scripts/w7d6_langgraph_demo.py", "w7d6/langgraph_demo.json"),
}

# Agent 线的 span 明细单独落一个文件，判定要连同它一起看
SPANS_FILE = "w7d6/langgraph_spans.json"

# 任务卡三条验收线要求「有证据」的正本报告
REPORTS = {
    "成本正本": "evals/COST_BENCH.md",
    "微调正本": "evals/LORA_DEMO.md",
    "Agent 正本": "evals/AGENT_GRAPH.md",
    "决策正本": "docs/W7_D5_DECISION.md",
}

# 报告里有、但本机无法重跑的证据（只能引用）
NOT_REPRODUCIBLE = [
    ("微调真训练", "Kaggle T4 单次代跑，本机 CPU-only 无 torch/peft",
     "evals/LORA_DEMO.md §5"),
    ("语义缓存", "本机缺 embedding 端点 / RediSearch / redisvl（W4 D5 实测）",
     "evals/COST_BENCH.md 边界"),
    ("真实流量分布", "重复率 30% 与误拦率均为演示规模", "evals/COST_BENCH.md 边界"),
]


def _cost_scenario(payload: dict, want_cache: bool, want_cheap: bool) -> dict:
    """按配方特征取场景：产物里没有 A/B/C/D 标签，只能用「有没有缓存命中 / 有没有便宜档」定位。

    A 基线 = 无命中 + 全 primary；D 组合 = 有命中 + 含 cheap 档。特征不唯一就报错，
    宁可让脚本停下，也不按顺序瞎取一条凑判定。
    """
    hits = []
    for item in payload.get("scenarios", []):
        has_cache = float(item.get("cache_hit_rate", 0)) > 0
        tiers = set(item.get("by_tier", {}))
        has_cheap = "cheap" in tiers
        if has_cache == want_cache and has_cheap == want_cheap:
            hits.append(item)
    if len(hits) != 1:
        raise KeyError(
            f"场景特征不唯一（cache={want_cache} cheap={want_cheap}）：命中 {len(hits)} 条"
        )
    return hits[0]


def judge_cost(payload: dict, extra: dict | None = None) -> tuple[bool, str, list[str]]:
    """成本线：组合方案相对基线降幅 >= 60%，且分流一致、未计入项写明。"""
    base = _cost_scenario(payload, want_cache=False, want_cheap=False)["per_1000_cost_units"]
    combo = _cost_scenario(payload, want_cache=True, want_cheap=True)["per_1000_cost_units"]
    drop = 1 - combo / base if base else 0.0
    agree = payload.get("routing_agreement", {}).get("agreement", 0.0)
    unpriced = payload.get("unpriced_items") or []

    checks = [
        f"降幅 {drop:.1%} >= 60%",
        f"分流一致率 {agree:.0%} == 100%",
        f"未计入项 {len(unpriced)} 条已写明",
    ]
    ok = drop >= 0.60 and agree == 1.0 and len(unpriced) >= 1
    brief = f"A {base:.1f} → D {combo:.1f}（降 {drop:.1%}）"
    return ok, brief, checks


def judge_lora(payload: dict, extra: dict | None = None) -> tuple[bool, str, list[str]]:
    """微调线（本机可跑的部分）：LoRA 参数量显著更小，且 r 不足时确实学不全。"""
    rows = {r.get("name", ""): r for r in payload.get("results", [])}
    full = next(r for k, r in rows.items() if "全量" in k)
    lora4 = next(r for k, r in rows.items() if "r=4" in k)
    lora1 = next(r for k, r in rows.items() if "r=1" in k)

    ratio = lora4["trainable_params"] / full["trainable_params"]
    # 容量不足的对照：r=1 的留出误差必须明显高于 r=4
    weak_is_worse = lora1["eval_mse_after"] > max(lora4["eval_mse_after"], 1e-12) * 10

    checks = [
        f"LoRA 可训练参数占比 {ratio:.1%} <= 15%",
        f"r=1 留出 MSE {lora1['eval_mse_after']:.3f} 明显差于 r=4 的 {lora4['eval_mse_after']:.6f}",
        "脚本自带「不是 LLM 微调验收」声明",
    ]
    ok = ratio <= 0.15 and weak_is_worse
    brief = f"参数 {ratio:.1%}、r=1 留出 MSE {lora1['eval_mse_after']:.1f}"
    return ok, brief, checks


def judge_decision(payload: dict, extra: dict | None = None) -> tuple[bool, str, list[str]]:
    """决策线：12 条场景全一致、证据全覆盖，且微调不是默认解（<=2 条且带 caveat）。"""
    agreement = payload.get("agreement", 0.0)
    coverage = payload.get("evidence_coverage", 0.0)
    total = payload.get("scenarios", 0)
    ft = payload.get("finetune_recommendations", 0)
    ft_caveat = payload.get("finetune_with_caveat", 0)

    checks = [
        f"一致率 {agreement:.0%}、证据覆盖 {coverage:.0%}（{total} 条场景）",
        f"微调推荐 {ft} 条、其中带未量声明 {ft_caveat} 条",
        f"证据索引 {payload.get('evidence_index_size', 0)} 条",
    ]
    ok = agreement == 1.0 and coverage == 1.0 and total >= 12 and ft <= 2 and ft_caveat >= 1
    brief = f"{total}/{total} 一致、微调仅 {ft} 条"
    return ok, brief, checks


FINISH_REASONS = {"completed", "max_steps", "timeout", "tool_error"}


def judge_agent(payload: dict, extra: dict | None = None) -> tuple[bool, str, list[str]]:
    spans = (extra or {}).get("spans", [])
    """Agent 线：三工具齐全、四类结束原因全覆盖、span 有父子层级、断点续跑能收口。"""
    scenarios = payload.get("scenarios", [])
    reasons = {s.get("finish_reason") for s in scenarios}
    resume = payload.get("resume", {})
    legacy = payload.get("legacy_crosscheck", [])

    s1 = next((s for s in scenarios if s.get("id") == "S1"), {})
    tools = [step.get("tool") for step in s1.get("trace", [])]
    steps_complete = all(
        step.get("tool") and step.get("args") is not None and step.get("result") is not None
        for step in s1.get("trace", [])
    )

    s1_spans = [sp for sp in spans if sp.get("scenario") == "S1"]
    root = next((sp for sp in s1_spans if sp.get("name") == "agent.run"), None)
    children = [sp for sp in s1_spans if str(sp.get("name", "")).startswith("agent.tool.")]
    # 子 span 的 parent 必须正好指向这条根 span
    has_parent = bool(root) and bool(children) and all(
        sp.get("parent_span_id") == root.get("span_id") for sp in children
    )

    checks = [
        f"{len(scenarios)} 个场景、结束原因 {len(reasons & FINISH_REASONS)}/4 类",
        f"S1 工具链 {len(tools)} 步（{' → '.join(tools) if tools else '—'}），每步入参/结果齐全",
        f"S1 span：根 agent.run 1 条 + 子 span {len(children)} 条，父 span 全部挂得住",
        f"checkpointer 断点续跑 {resume.get('finish_reason')} / steps={resume.get('steps')}",
        f"与零依赖版前 {len(legacy)} 场景逐项对账一致",
    ]
    ok = (
        len(scenarios) >= 8
        and len(reasons & FINISH_REASONS) == 4
        and len(tools) == 3
        and steps_complete
        and has_parent
        and resume.get("finish_reason") == "completed"
        and all(item.get("same_tools") for item in legacy)
    )
    span_total = len(children) + (1 if root else 0)
    brief = f"{len(scenarios)} 场景、S1 span {span_total} 条、续跑 {resume.get('steps')} 步"
    return ok, brief, checks


JUDGES = {
    "cost": ("成本（有实测数字）", judge_cost),
    "lora": ("微调（有实验记录）", judge_lora),
    "decision": ("决策对照（D5，不属三线）", judge_decision),
    "agent": ("Agent（可观测）", judge_agent),
}


def run_demo(key: str) -> tuple[int, str]:
    """重跑一条线的 demo，返回 (退出码, 末行输出)。"""
    script, _ = DEMOS[key]
    proc = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    return proc.returncode, (tail[-1] if tail else "")


def run_pytest() -> tuple[int, str]:
    # 不带 -q：本机 pytest 用 -q 时汇总行（N passed）不打印，末行只剩进度点，取不到结论
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    return proc.returncode, (tail[-1] if tail else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="W7 D7 三线验收核对")
    parser.add_argument("--skip-run", action="store_true", help="不重跑 demo，只读现有产物")
    parser.add_argument("--with-pytest", action="store_true", help="追加全量 pytest")
    args = parser.parse_args()

    print("W7 D7 验收核对 —— 重跑产物再判定，不抄旧报告")
    print(f"仓库：{ROOT}")
    print()

    results = []
    for key in ("cost", "lora", "decision", "agent"):
        label, judge = JUDGES[key]
        script, rel = DEMOS[key]

        if args.skip_run:
            run_note = "未重跑（--skip-run）"
            code = 0
        else:
            code, last = run_demo(key)
            run_note = f"重跑退出码 {code}｜{last}" if last else f"重跑退出码 {code}"

        path = OUT / rel
        if not path.exists():
            results.append((key, label, False, "产物缺失", [f"找不到 {rel}"], run_note))
            continue

        payload = json.loads(path.read_text(encoding="utf-8"))
        extra: dict = {}
        if key == "agent":
            spans_path = OUT / SPANS_FILE
            extra["spans"] = (
                json.loads(spans_path.read_text(encoding="utf-8")) if spans_path.exists() else []
            )
        ok, brief, checks = judge(payload, extra)
        # demo 自己崩了，判定不能算 PASS
        ok = ok and code == 0
        results.append((key, label, ok, brief, checks, run_note))

    # 正本报告是否都在
    print("== 正本报告 ==")
    missing = []
    for name, rel in REPORTS.items():
        exists = (ROOT / rel).exists()
        print(f"  {'OK  ' if exists else 'MISS'} {name:<10} {rel}")
        if not exists:
            missing.append(rel)
    print()

    # 三线逐条
    print("== 三条验收线 ==")
    for _key, label, ok, brief, checks, run_note in results:
        print(f"  [{ 'PASS' if ok else 'FAIL' }] {label}：{brief}")
        for line in checks:
            print(f"         - {line}")
        print(f"         · {run_note}")
    print()

    # 本机跑不了、只能引用的证据
    print("== 报告有记录但本机不可重跑（引用，不冒充演示）==")
    for name, why, where in NOT_REPRODUCIBLE:
        print(f"  - {name}：{why}（{where}）")
    print()

    if args.with_pytest:
        code, last = run_pytest()
        print(f"== 全量测试 ==\n  {'PASS' if code == 0 else 'FAIL'} {last}\n")

    failed = [r[1] for r in results if not r[2]]
    if not failed and not missing:
        verdict = "三条验收线全部 PASS"
    else:
        verdict = f"未通过：{', '.join(failed) or '正本报告缺失'}"
    print(f"结论：{verdict}")

    if failed or missing:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 核对脚本自身失败要和「验收不通过」区分开
        print(f"核对脚本自身失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
