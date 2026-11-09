"""W7 D4 · 真 LoRA 微调脚本骨架（供 Colab / 短时云 GPU 执行）。

> [!warning] 本机**未执行**、不算 D4 验收
> 2026-11-09 实测：本机 CPU-only（无 `nvidia-smi`），且 `torch` / `peft` /
> `transformers` / `datasets` 全部未安装。任务卡 11/19 也说「本机无 GPU，
> 别硬扛」。所以这份脚本是**留给云端跑的可执行清单**，不是已完成的实验。
> 在本机直接运行会在 preflight 阶段以退出码 2 明确停下，不会假装跑成功。

跑完请把真实数字回写到 `evals/LORA_DEMO.md` 的「云端补跑」小节：
  基座模型与数据集、LoRA r / alpha / target_modules、训练耗时与显存、
  训练前后在同一留出集上的格式符合率、adapter 产物体积与存放位置。

Colab 上先装依赖（T4 免费档约十几分钟）：
  !pip install -q "torch>=2.1" "transformers>=4.40" "peft>=0.10" "datasets>=2.18" accelerate

然后：
  !python w7d4_lora_colab.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 刻意选小模型：demo 级目标是「跑通一遍流水线」，不是训一个能用的模型。
BASE_MODEL = os.getenv("LORA_BASE_MODEL", "sshleifer/tiny-gpt2")
RANK = int(os.getenv("LORA_R", "4"))
ALPHA = int(os.getenv("LORA_ALPHA", "16"))
EPOCHS = int(os.getenv("LORA_EPOCHS", "3"))
MAX_STEPS = int(os.getenv("LORA_MAX_STEPS", "60"))
OUT_DIR = Path(os.getenv("LORA_OUT_DIR", "evals/out/w7d4/adapter"))

# 玩具指令数据：让模型学会「按固定字段回答」这种稳定行为，
# 而不是记住会变化的事实 —— 后者应该交给 RAG（见 D5 决策对照表）。
TRAIN_EXAMPLES: list[dict[str, str]] = [
    {
        "prompt": "问：报销截止日？\n答：",
        "completion": "字段=截止日；值=每月 25 日；依据=制度第 3 条。",
    },
    {
        "prompt": "问：谁审批？\n答：",
        "completion": "字段=审批人；值=部门主管；依据=制度第 5 条。",
    },
    {
        "prompt": "问：发票要求？\n答：",
        "completion": "字段=发票；值=增值税专用；依据=制度第 7 条。",
    },
    {
        "prompt": "问：超标怎么办？\n答：",
        "completion": "字段=超标；值=需附说明；依据=制度第 9 条。",
    },
]
HOLDOUT_PROMPTS = [
    "问：差旅标准？\n答：",
    "问：归档期限？\n答：",
    "问：对公账户？\n答：",
]


def preflight() -> None:
    """缺 GPU / 缺依赖就明确停，不用 mock 冒充训练。"""
    missing: list[str] = []
    for name in ("torch", "transformers", "peft", "datasets", "accelerate"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        print(f"[preflight] 缺少依赖：{', '.join(missing)}")
        print("[preflight] 本机无法执行真微调；这不是失败，是环境边界。退出码 2。")
        sys.exit(2)

    import torch

    if not torch.cuda.is_available():
        print("[preflight] 有依赖但没有 CUDA GPU；QLoRA/LoRA 训练需要云 GPU 或 Colab T4。")
        sys.exit(2)
    print(f"[preflight] OK：{torch.cuda.get_device_name(0)}")


def format_ok(text: str) -> bool:
    """留出集评估：不看内容对错，只看有没有学会固定字段格式。"""
    return all(k in text for k in ("字段=", "值=", "依据="))


def main() -> None:
    preflight()

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL)

    lora_config = LoraConfig(
        r=RANK,
        lora_alpha=ALPHA,
        target_modules=["c_attn"],  # tiny-gpt2 的注意力投影层；换基座要改这里
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"[setup] 可训练参数 {trainable} / 总参数 {total} = {trainable / total:.2%}")

    def encode(example: dict[str, str]) -> dict:
        text = example["prompt"] + example["completion"] + tokenizer.eos_token
        return tokenizer(text, truncation=True, max_length=128)

    ds = Dataset.from_list(TRAIN_EXAMPLES).map(encode, remove_columns=["prompt", "completion"])

    def generate(prompt: str) -> str:
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=48, do_sample=False)
        return tokenizer.decode(out[0], skip_special_tokens=True)

    print("[eval] 训练前留出集格式符合率：", end="")
    before = [format_ok(generate(p)) for p in HOLDOUT_PROMPTS]
    print(f"{sum(before)}/{len(before)}")

    args = TrainingArguments(
        output_dir=str(OUT_DIR / "trainer"),
        num_train_epochs=EPOCHS,
        max_steps=MAX_STEPS,
        per_device_train_batch_size=2,
        learning_rate=2e-4,
        logging_steps=10,
        save_strategy="no",
        report_to=[],  # 不接观测后端，避免把云端 key 写进环境
        fp16=torch.cuda.is_available(),
    )
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=ds,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
    )
    started = time.perf_counter()
    trainer.train()
    elapsed = round(time.perf_counter() - started, 2)

    print("[eval] 训练后留出集格式符合率：", end="")
    after = [format_ok(generate(p)) for p in HOLDOUT_PROMPTS]
    print(f"{sum(after)}/{len(after)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(OUT_DIR))
    record = {
        "base_model": BASE_MODEL,
        "rank": RANK,
        "alpha": ALPHA,
        "epochs": EPOCHS,
        "max_steps": MAX_STEPS,
        "trainable_params": trainable,
        "total_params": total,
        "train_examples": len(TRAIN_EXAMPLES),
        "holdout_before": sum(before),
        "holdout_after": sum(after),
        "holdout_total": len(HOLDOUT_PROMPTS),
        "train_seconds": elapsed,
        "gpu": torch.cuda.get_device_name(0),
        "adapter_dir": str(OUT_DIR),
        "note": "真机实跑结果；请回写 evals/LORA_DEMO.md 的「云端补跑」小节",
    }
    (OUT_DIR / "run_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[done] 记录：{OUT_DIR / 'run_record.json'}")


if __name__ == "__main__":
    main()
