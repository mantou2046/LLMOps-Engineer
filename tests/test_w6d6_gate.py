"""W6 D6 非 LLM eval 门禁的单测：正常放行，改坏 query 必须被拦下。

对应任务卡 11/14 的验收「故意改坏 prompt 的 PR 被 CI 拦下」。
全程只跑本地脚本，不调 LLM、不联网、不装额外依赖。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "w6d6_eval_gate.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


def test_gate_passes_on_clean_template():
    result = run()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PASS" in result.stdout


def test_gate_blocks_broken_prompt(tmp_path: Path):
    """把 {question} 占位符去掉，检索拿不到题干，召回掉破阈值。"""
    broken = tmp_path / "broken_template.txt"
    broken.write_text("请回答一个固定的无关问题", encoding="utf-8")
    result = run("--template", str(broken))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL" in result.stdout


def test_self_test_reports_both_directions():
    result = run("--self-test")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "正常放行、改坏被拦" in result.stdout
