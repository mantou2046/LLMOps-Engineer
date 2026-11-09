"""W7 D4 · LoRA 低秩适配的最小可跑演示（纯 numpy，CPU，秒级）。

⚠️⚠️ 这不是「跑了一次 LLM 微调」，别拿它冒充任务卡 11/19 的完整验收：
  · 本机无 GPU，`torch` / `peft` / `transformers` / `datasets` 全部没装
    （2026-11-09 实测），真 LoRA / QLoRA 训练在本机跑不起来。
  · 本文件是一个**线性层上的数学等价演示**：冻结基础权重 W₀，只训练
    低秩增量 ΔW = B@A，用真实梯度下降把 loss 降下来，量参数量与前后误差。
    没有 Transformer、没有注意力、没有分词、没有量化、没有真实语言数据。
  · 目标是**刻意构造成低秩**的（见 make_dataset），所以 r=4 能追平全量微调；
    这正是 LoRA 的核心假设「微调更新是低秩的」，真实任务不保证成立。
  · 云端真微调的可执行骨架在 `notebooks/w7d4_lora_colab.py`，
    **本机未执行、不算验收**。

能讲清的流水线地图（DeepLearning.AI《LLMOps》那条线）：
  数据 → 冻结基线 → 训练（只更新 adapter）→ 留出集评估 → 产物（adapter 权重）
本文件把每一步都做成可调用的函数，脚本 `scripts/w7d4_lora_demo.py` 串起来出数字。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

SEED = 20261119
DIM = 64
TRUE_RANK = 4


@dataclass(frozen=True)
class Dataset:
    w0: np.ndarray       # 冻结的「基础模型」权重 (d, d)
    delta_target: np.ndarray  # 真实需要的增量，刻意做成低秩 (d, d)
    x_train: np.ndarray
    y_train: np.ndarray
    x_eval: np.ndarray
    y_eval: np.ndarray


@dataclass(frozen=True)
class TrainResult:
    name: str
    trainable_params: int
    rank: int | None
    learning_rate: float
    loss_before: float
    loss_after: float
    eval_mse_before: float
    eval_mse_after: float
    epochs: int
    seconds: float
    adapter_bytes: int
    history: list[float]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "trainable_params": self.trainable_params,
            "rank": self.rank,
            "learning_rate": self.learning_rate,
            "loss_before": round(self.loss_before, 6),
            "loss_after": round(self.loss_after, 6),
            "eval_mse_before": round(self.eval_mse_before, 6),
            "eval_mse_after": round(self.eval_mse_after, 6),
            "reduction": round(
                1 - self.eval_mse_after / self.eval_mse_before, 4
            ),
            "epochs": self.epochs,
            "seconds": round(self.seconds, 4),
            "adapter_bytes": self.adapter_bytes,
            "history": [round(v, 6) for v in self.history],
        }


def make_dataset(
    dim: int = DIM,
    true_rank: int = TRUE_RANK,
    n_train: int = 200,
    n_eval: int = 50,
    seed: int = SEED,
    noise: float = 0.01,
) -> Dataset:
    """造一个「学一种稳定输出变换」的玩具任务：y ≈ (W₀ + ΔW_target) x。

    ΔW_target = U@V 是真秩 true_rank 的低秩矩阵 —— 用来演示
    「r 够大能装下、r 太小装不下」，不表示真实微调一定低秩。
    """
    rng = np.random.default_rng(seed)
    w0 = rng.normal(0, 0.1, (dim, dim)).astype(np.float32)
    u = rng.normal(0, 1.0, (dim, true_rank)).astype(np.float32)
    v = rng.normal(0, 1.0, (true_rank, dim)).astype(np.float32)
    delta_target = u @ v
    w_target = w0 + delta_target

    x_train = rng.normal(0, 1.0, (n_train, dim)).astype(np.float32)
    x_eval = rng.normal(0, 1.0, (n_eval, dim)).astype(np.float32)
    y_train = x_train @ w_target.T + rng.normal(0, noise, (n_train, dim)).astype(np.float32)
    y_eval = x_eval @ w_target.T + rng.normal(0, noise, (n_eval, dim)).astype(np.float32)
    return Dataset(w0, delta_target, x_train, y_train, x_eval, y_eval)


def mse(pred: np.ndarray, target: np.ndarray) -> float:
    return float(np.mean((pred - target) ** 2))


def predict(w0: np.ndarray, delta: np.ndarray, x: np.ndarray) -> np.ndarray:
    return x @ (w0 + delta).T


def train_full(
    ds: Dataset, *, lr: float = 0.05, epochs: int = 400
) -> TrainResult:
    """对照组：全量微调，直接训练整个 ΔW（d×d 个可训练参数）。"""
    import time

    dim = ds.w0.shape[0]
    delta = np.zeros((dim, dim), dtype=np.float32)
    start = time.perf_counter()
    history: list[float] = []
    for _ in range(epochs):
        pred = predict(ds.w0, delta, ds.x_train)
        history.append(mse(pred, ds.y_train))
        grad = 2.0 * (pred - ds.y_train).T @ ds.x_train / len(ds.x_train)
        delta -= lr * grad
    seconds = time.perf_counter() - start
    final_pred = predict(ds.w0, delta, ds.x_train)
    history.append(mse(final_pred, ds.y_train))
    payload = json.dumps({"delta": delta.tolist()}).encode("utf-8")
    return TrainResult(
        name="全量微调（训练整个 ΔW）",
        trainable_params=dim * dim,
        rank=None,
        learning_rate=lr,
        loss_before=history[0],
        loss_after=history[-1],
        eval_mse_before=mse(predict(ds.w0, np.zeros_like(delta), ds.x_eval), ds.y_eval),
        eval_mse_after=mse(predict(ds.w0, delta, ds.x_eval), ds.y_eval),
        epochs=epochs,
        seconds=seconds,
        adapter_bytes=len(payload),
        history=history,
    )


def train_lora(
    ds: Dataset, *, rank: int = 4, lr: float = 0.002, epochs: int = 400
) -> TrainResult:
    """LoRA：W₀ 全程冻结，只训练 B@A 两个小矩阵。

    B 初始化为 0（PEFT 的标准做法）→ 起点 ΔW=0，训练前后对比才公平；
    A 用小随机值。梯度用链式法则手推，不调任何自动微分框架。

    ⚠️ 学习率不能照抄全量微调：双线性参数化的梯度会被 A / B 互相放大，
       实测 lr=0.005 起就 NaN（全量微调 lr=0.05 仍稳）。0.002 是扫过的
       稳定区间，两路各取能收敛的步长、统一 400 轮再比最终误差。
    """
    import time

    dim = ds.w0.shape[0]
    rng = np.random.default_rng(SEED + 1)
    a = rng.normal(0, 0.01, (rank, dim)).astype(np.float32)
    b = np.zeros((dim, rank), dtype=np.float32)
    start = time.perf_counter()
    history: list[float] = []
    for _ in range(epochs):
        delta = b @ a
        pred = predict(ds.w0, delta, ds.x_train)
        history.append(mse(pred, ds.y_train))
        g = 2.0 * (pred - ds.y_train).T @ ds.x_train / len(ds.x_train)  # dL/d(ΔW)
        grad_b = g @ a.T
        grad_a = b.T @ g
        b -= lr * grad_b
        a -= lr * grad_a
    seconds = time.perf_counter() - start
    delta = b @ a
    history.append(mse(predict(ds.w0, delta, ds.x_train), ds.y_train))
    # adapter 只存 A / B，不存 W₀ —— 这就是「可插拔」的含义
    payload = json.dumps({"A": a.tolist(), "B": b.tolist()}).encode("utf-8")
    zeros = np.zeros((dim, dim), dtype=np.float32)
    return TrainResult(
        name=f"LoRA r={rank}（只训练 B@A）",
        trainable_params=rank * dim + dim * rank,
        rank=rank,
        learning_rate=lr,
        loss_before=history[0],
        loss_after=history[-1],
        eval_mse_before=mse(predict(ds.w0, zeros, ds.x_eval), ds.y_eval),
        eval_mse_after=mse(predict(ds.w0, delta, ds.x_eval), ds.y_eval),
        epochs=epochs,
        seconds=seconds,
        adapter_bytes=len(payload),
        history=history,
    )
