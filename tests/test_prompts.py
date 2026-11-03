"""app/prompts.py 的加载单测。

只测「取版 / 指针 / hash / 回落 reason」这套纯本地逻辑（load_prompt_local），
不碰 registry（那要真网络）。保证改 prompt 版本时不会悄悄给错版。
"""

from __future__ import annotations

import prompts


def test_local_exact_version():
    """指定 version=1 → 拿到 v1，hash 与 source 正确。

    ⚠️ source 是 `Path.relative_to` 拼的，Windows 上路径分隔符是反斜杠
    而非正斜杠，别用正斜杠断言写死 —— 只断言「以 local:prompts 开头 + 含正确文件名」。
    """
    got = prompts.load_prompt_local("structured_json", version=1)
    assert got["version"] == 1
    assert got["source"].startswith("local:prompts")
    assert got["source"].endswith("structured_json/v1.yaml") or got["source"].endswith("structured_json\\v1.yaml")
    assert len(got["hash"]) == 8  # sha256 前 8 位
    assert got["reason"] is None


def test_local_version_hash_matches_content():
    """hash 是对 template 正文算的，不是对文件整体。"""
    got = prompts.load_prompt_local("structured_json", version=1)
    expect = prompts.prompt_hash(got["template"])
    assert got["hash"] == expect


def test_local_by_label_resolves_pointer():
    """不指定 version、给 label → 走 labels.yaml 指针，不猜最新版。"""
    # production 指向哪版由 prompts/structured_json/labels.yaml 决定，
    # 测试只断言「能取到」且 source 是 local，不钉死具体版号（会随发布变）。
    got = prompts.load_prompt_local("structured_json", label="production")
    assert got["source"].startswith("local:")
    assert got["version"] >= 1


def test_local_label_falls_back_to_latest():
    """label 在 labels.yaml 里查不到 → 回落到最新版，且 local_pick 说明是 latest。"""
    got = prompts.load_prompt_local("structured_json", label="no-such-label")
    assert got["source"].startswith("local:")
    assert "latest" in (got["local_pick"] or "")


def test_local_unknown_name_raises():
    """本地没有任何版本 → 明确抛 FileNotFoundError，绝不静默返回空串。"""
    try:
        prompts.load_prompt_local("does-not-exist", version=1)
        raise AssertionError("应当抛 FileNotFoundError，但没抛")
    except FileNotFoundError:
        pass
