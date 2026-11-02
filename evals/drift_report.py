"""W5 D2 · 数据漂移报告（Evidently 0.7）。

一份「特征表」+ 两次对比 = 一张 HTML 报告 + 一行行打印的判定表。

跑法（在仓库根，用 D:\\python314 的解释器，evidently 装在那儿）：

    D:\\python314\\python.exe evals/drift_report.py --scenario control
    D:\\python314\\python.exe evals/drift_report.py --scenario coldstart
    D:\\python314\\python.exe evals/drift_report.py --scenario injected

    # 换成自己的两份数据（同一套列名即可）
    D:\\python314\\python.exe evals/drift_report.py --reference a.parquet --current b.parquet

三个内置场景都用 evals/out/batch_mock_w8.parquet（100 行 mock 批处理产出）当底料：

    control    同一份文件随机切半      → 期望「不漂」，用来看假阳性
    coldstart  前 50 行 vs 后 50 行    → 前 50 是冷启动段，延迟真的更高
    injected   人为改延迟 / 档位 / 问题长度 → 期望「明显漂」

⚠️ 这批数据的 latency_ms 是 mock 上游模拟出来的（0.05–0.4 ms），
   所以「延迟漂移」在这份数据上只能证明**管道通了**，不能当业务结论用。
"""

from __future__ import annotations

import argparse
import json
import re
import warnings
from pathlib import Path

import pandas as pd
from evidently import Report
from evidently.presets import DataDriftPreset

OUT_DIR = Path("evals/out")
DEFAULT_SOURCE = OUT_DIR / "batch_mock_w8.parquet"

# 进漂移报告的特征列。刻意不含 id / request_id / 时间戳 ——
# 这些列的取值个数随请求增长，放进来只会让报告变成「每列都漂」。
FEATURES = [
    "question_chars",
    "answer_chars",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms",
    "tier",
]


def to_features(df: pd.DataFrame) -> pd.DataFrame:
    """原始批次表 → 漂移特征表。只保留「会被业务变化影响」的列。"""
    out = pd.DataFrame(index=df.index)
    if "question" in df:
        out["question_chars"] = df["question"].astype(str).str.len()
    if "answer" in df:
        out["answer_chars"] = df["answer"].astype(str).str.len()
    for col in ("prompt_tokens", "completion_tokens", "latency_ms"):
        if col in df:
            out[col] = pd.to_numeric(df[col], errors="coerce")
    if "tier" in df:
        out["tier"] = df["tier"].astype(str)
    missing = [c for c in FEATURES if c not in out]
    if missing:
        print(f"[warn] 缺列（报告里不会有）：{', '.join(missing)}")
    return out.dropna(axis=1, how="all")


def load(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    return pd.read_parquet(path)


def build_pair(scenario: str, source: Path) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """返回 (reference, current, 场景说明)。"""
    df = load(source)
    if scenario == "control":
        # 随机切半：同分布、同时间窗，唯一差别是抽样噪声
        shuffled = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
        return shuffled.head(50), shuffled.tail(50), "随机切半（阴性对照）"
    if scenario == "coldstart":
        # 前 50 / 后 50：顺序切，不洗牌 —— 前一半含冷启动
        return df.head(50), df.tail(50), "前 50 vs 后 50（含冷启动段）"
    if scenario == "injected":
        cur = df.copy()
        cur["latency_ms"] = cur["latency_ms"] * 40 + 500      # 延迟整体抬 40 倍
        cur["tier"] = "cheap"                                  # 档位全切到便宜档
        cur["question"] = cur["question"] + "（请详细展开，并给出至少三个例子）" * 2
        return df, cur, "人为注入（延迟 ×40 / 档位改 cheap / 问题加长）"
    raise SystemExit(f"未知场景：{scenario}（可选 control / coldstart / injected）")


def parse_metric(metric_name: str) -> tuple[str, str]:
    """从 'ValueDrift(column=latency_ms,method=K-S p_value,threshold=0.05)' 里取列名与方法。"""
    col = re.search(r"column=([^,]+)", metric_name)
    method = re.search(r"method=([^,]+)", metric_name)
    return (col.group(1) if col else "?", method.group(1) if method else "?")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="control",
                    choices=["control", "coldstart", "injected"])
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--reference")
    ap.add_argument("--current")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.reference and args.current:
        ref_raw, cur_raw = load(Path(args.reference)), load(Path(args.current))
        label = f"{Path(args.reference).stem} vs {Path(args.current).stem}"
        tag = "custom"
    else:
        ref_raw, cur_raw, label = build_pair(args.scenario, Path(args.source))
        tag = args.scenario

    ref, cur = to_features(ref_raw), to_features(cur_raw)
    print(f"场景：{label}")
    print(f"样本量：reference {len(ref)} 行 / current {len(cur)} 行；特征 {len(ref.columns)} 列\n")

    # Evidently 用 scipy 的卡方/Z 检验。某类别只在一侧出现时，期望频数为 0，
    # scipy 会抛 "divide by zero encountered in divide" 并把 p 算成 0。
    # 这个警告本身是「检验退化」的信号（下面会单独标出来），不该刷屏。
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="divide by zero")
        snapshot = Report([DataDriftPreset()]).run(reference_data=ref, current_data=cur)

    rows, drifted, drift_share = [], 0, 0.5
    for m in snapshot.dict()["metrics"]:
        name, value = m["metric_name"], m["value"]
        if name.startswith("DriftedColumnsCount"):
            # 数据集级判定阈值：默认「50% 的列漂了」才算整体漂移
            drift_share = float(m.get("config", {}).get("drift_share", 0.5))
            continue
        col, method = parse_metric(name)
        p = float(value)
        is_drift = p < 0.05
        drifted += is_drift
        # p 恰好为 0 基本只有一种成因：某个类别只在一侧出现，期望频数为 0，
        # 卡方/Z 检验的 (obs-exp)**2/exp 里除了零 → scipy 抛 RuntimeWarning 并给 p=0。
        # 这种 p 不能读成「差异极显著」，只能读成「检验退化了，换指标看」。
        degenerate = p == 0.0
        rows.append((col, method, p, is_drift, degenerate))

    print(f"{'列':<20}{'检验方法':<14}{'p 值':>12}   判定")
    print("-" * 66)
    for col, method, p, is_drift, degenerate in sorted(rows, key=lambda r: r[2]):
        note = "  ⚠️ p=0 检验退化（有类别只在一侧）" if degenerate else ""
        print(f"{col:<20}{method:<14}{p:>12.3e}   {'漂移' if is_drift else '正常'}{note}")

    n_cols = len(rows)
    share = drifted / n_cols if n_cols else 0.0
    dataset_drift = share >= drift_share
    print("-" * 66)
    print(f"合计 {drifted}/{n_cols} 列漂移（{share:.1%}）")
    print(f"数据集判定：{'漂移' if dataset_drift else '未漂移'}"
          f"（阈值 = {drift_share:.0%} 的列；{share:.1%} {'≥' if dataset_drift else '<'} {drift_share:.0%}）")

    html = out_dir / f"drift_{tag}.html"
    snapshot.save_html(str(html))
    print(f"HTML 报告：{html}")

    summary = {
        "scenario": tag,
        "label": label,
        "reference_rows": len(ref),
        "current_rows": len(cur),
        "columns": n_cols,
        "drifted_columns": drifted,
        "drifted_share": round(share, 4),
        "drift_share_threshold": drift_share,
        "dataset_drift": dataset_drift,
        "per_column": [
            {"column": c, "method": m, "p_value": p, "drift": d, "degenerate": g}
            for c, m, p, d, g in rows
        ],
    }
    js = out_dir / f"drift_{tag}.json"
    js.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 摘要：{js}")


if __name__ == "__main__":
    main()
