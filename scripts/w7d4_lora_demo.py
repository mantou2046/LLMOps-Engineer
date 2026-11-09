"""W7 D4 · 低秩适配最小实跑：全量微调 vs LoRA r=4 vs LoRA r=1。

⚠️ 纯 numpy 的**线性层教学演示**，不是 LLM 微调。本机无 GPU、缺
   torch / peft / transformers，真 LoRA 训练跑不起来；本脚本只用来把
   「冻结基线 → 只训练低秩增量 → 留出集评估 → 产物体积」这条流水线
   跑出可核对的前后数字。云端真微调骨架见 notebooks/w7d4_lora_colab.py（未执行）。

用法（仓库根目录）：
  D:\\python314\\python.exe scripts\\w7d4_lora_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import lora_demo  # noqa: E402

OUT = ROOT / "evals" / "out" / "w7d4" / "lora_demo.json"


def main() -> None:
    ds = lora_demo.make_dataset()
    results = [
        lora_demo.train_full(ds, lr=0.05, epochs=400),
        lora_demo.train_lora(ds, rank=4, lr=0.002, epochs=400),
        lora_demo.train_lora(ds, rank=1, lr=0.002, epochs=400),
    ]
    baseline = results[0]

    payload = {
        "seed": lora_demo.SEED,
        "dim": lora_demo.DIM,
        "true_rank_of_target": lora_demo.TRUE_RANK,
        "train_samples": len(ds.x_train),
        "eval_samples": len(ds.x_eval),
        "epochs": baseline.epochs,
        "learning_rate_note": (
            "全量微调 lr=0.05 稳；LoRA 是双线性 B@A 参数化，lr>=0.005 实测 NaN，"
            "故取 0.002。两路各取能稳定收敛的步长、统一 400 轮再比最终误差。"
        ),
        "results": [r.as_dict() for r in results],
        "what_this_is": [
            "纯 numpy 线性层教学演示，不是 Transformer / LLM 微调",
            "目标增量刻意构造成真秩 4 的低秩矩阵，因此 r=4 能追平全量微调",
            "真实微调不保证更新低秩；本演示的数字不能外推到语言模型",
            "本机无 GPU 且缺 torch/peft/transformers/datasets，云端脚本未执行",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "方法                          可训练参数  lr      训练loss 前→后      "
        "留出MSE 前→后     下降    耗时(s) 产物字节"
    )
    for r in results:
        print(
            f"{r.name:<28} {r.trainable_params:>9d}  {r.learning_rate:<6} "
            f"{r.loss_before:>9.3f}→{r.loss_after:<9.3f} "
            f"{r.eval_mse_before:>9.3f}→{r.eval_mse_after:<9.3f} "
            f"{1 - r.eval_mse_after / r.eval_mse_before:>7.1%} "
            f"{r.seconds:>7.3f} {r.adapter_bytes:>9d}"
        )

    full, r4, r1 = results
    print(f"\n参数量：LoRA r=4 是全量微调的 {r4.trainable_params / full.trainable_params:.1%}；"
          f"产物体积 {r4.adapter_bytes / full.adapter_bytes:.1%}")
    print(f"容量不足对照：r=1 的留出 MSE {r1.eval_mse_after:.3f} vs r=4 的 {r4.eval_mse_after:.3f}")

    assert full.eval_mse_after < full.eval_mse_before * 0.1, "全量微调必须真的收敛"
    assert r4.eval_mse_after < r4.eval_mse_before * 0.1, "LoRA r=4 必须真的收敛"
    assert r4.trainable_params < full.trainable_params, "LoRA 参数量必须更少"
    assert r4.adapter_bytes < full.adapter_bytes, "adapter 产物必须更小"
    assert r1.eval_mse_after > r4.eval_mse_after, "r=1 装不下真秩 4 的目标，误差应更大"
    print("\n断言 5/5 通过：两路都收敛、LoRA 参数与产物更小、r 太小确实学不全。")
    print(f"明细：{OUT.relative_to(ROOT)}")
    print("\n⚠️ 这不是 LLM 微调验收：真 LoRA/QLoRA 需云 GPU，")
    print("   见 notebooks/w7d4_lora_colab.py（本机未执行）。")


if __name__ == "__main__":
    main()
