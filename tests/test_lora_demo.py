"""W7 D4 · 低秩适配教学演示单测（纯 numpy，CPU，零 GPU 依赖）。

同时验证一件要紧的事：云端真微调脚本在本机**必须明确停下**，
不能用 mock 或假数字冒充「跑过一次微调」。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import lora_demo
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ds():
    return lora_demo.make_dataset()


def test_dataset_shapes_and_determinism(ds):
    assert ds.w0.shape == (lora_demo.DIM, lora_demo.DIM)
    assert ds.x_train.shape == (200, lora_demo.DIM)
    assert ds.x_eval.shape == (50, lora_demo.DIM)
    again = lora_demo.make_dataset()
    assert np.allclose(ds.y_train, again.y_train)
    assert np.allclose(ds.delta_target, again.delta_target)


def test_target_is_low_rank(ds):
    """目标增量刻意是真秩 4 —— 这是 r=4 能追平全量微调的前提。"""
    rank = int(np.linalg.matrix_rank(ds.delta_target, tol=1e-3))
    assert rank == lora_demo.TRUE_RANK


def test_predict_shape(ds):
    zeros = np.zeros_like(ds.w0)
    assert lora_demo.predict(ds.w0, zeros, ds.x_eval).shape == ds.y_eval.shape


def test_full_finetune_converges(ds):
    r = lora_demo.train_full(ds, lr=0.05, epochs=400)
    assert r.trainable_params == lora_demo.DIM**2  # 4096
    assert r.eval_mse_after < r.eval_mse_before * 0.1
    assert r.loss_after < r.loss_before * 0.1
    assert r.learning_rate == 0.05


def test_lora_rank4_converges_with_fewer_params(ds):
    full = lora_demo.train_full(ds, lr=0.05, epochs=400)
    r4 = lora_demo.train_lora(ds, rank=4, lr=0.002, epochs=400)
    assert r4.trainable_params == 4 * lora_demo.DIM * 2  # 512
    assert r4.trainable_params < full.trainable_params
    assert r4.adapter_bytes < full.adapter_bytes
    assert r4.eval_mse_after < r4.eval_mse_before * 0.1
    assert r4.adapter_bytes > 0


def test_rank1_cannot_fit_rank4_target(ds):
    r4 = lora_demo.train_lora(ds, rank=4, lr=0.002, epochs=400)
    r1 = lora_demo.train_lora(ds, rank=1, lr=0.002, epochs=400)
    assert r1.trainable_params < r4.trainable_params
    assert r1.eval_mse_after > r4.eval_mse_after


def test_lora_learning_rate_too_large_diverges(ds):
    """双线性参数化照抄全量微调的 lr 会 NaN —— 实测过的坑要留一条断言。"""
    with np.errstate(over="ignore", invalid="ignore"):
        r = lora_demo.train_lora(ds, rank=4, lr=0.05, epochs=400)
    assert np.isnan(r.loss_after) or r.loss_after > 1.0


def test_colab_script_refuses_to_fake_a_run():
    """没有 GPU / 缺依赖时，云端骨架必须以退出码 2 明确停下。"""
    if importlib.util.find_spec("torch") is not None:
        pytest.skip("本机已装 torch，preflight 的环境边界不再成立")
    script = ROOT / "notebooks" / "w7d4_lora_colab.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert "缺少依赖" in proc.stdout
    assert "这不是失败" in proc.stdout
