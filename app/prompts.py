"""app/prompts.py —— prompt 的加载层：registry 优先，失败回落本地文件。

设计取舍（DEC-027）：
  · git 是唯一真源（prompts/**/*.yaml）
  · registry 只是发布指针（Langfuse label / MLflow alias）
  · 加载失败绝不炸服务：回落本地 + 记 reason（与 app/combos.py 同一思路）

返回三元组 + 溯源字段：
  {"template": str, "version": int, "hash": str,
   "source": str, "reason": str|None, "local_pick": str|None}
  · hash = 模板正文 sha256 前 8 位（与 scripts/track_prompt_run.py 同口径）
  · source = "langfuse" / "local:prompts/structured_json/v1.yaml"
  · reason = 回落时记录的原因（静默回落比不回落更危险）
  · local_pick = 回落时「凭什么叫这一版」——label→v1 / latest=v2，必须写清楚

⚠️ 2026-10-21 实测修正两处（四条路径跑出来的真问题）：
  1) 本模块**自己加载 .env**：此前依赖调用方 load_dotenv，任何直接 import 的脚本
     都会拿到空 public_key → Langfuse client disabled → 四条路径全部静默回落。
     判据就是日志里那句 `Authentication error: ... initialized without public_key`。
  2) 回落**不能只取最新版**：label=production 实际指向 v1，但「最新文件」是 v2 →
     会静默给错版本。改为先读 prompts/<name>/labels.yaml 还原指针，取不到才用最新版，
     并把「凭什么叫这一版」写进 local_pick。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

# ⚠️ 代理预检（D15 坑九）：HTTP_PROXY 指向 127.0.0.1 且无 no_proxy 时，
#    Langfuse 请求会被本地代理截胡 → 随机 404 / 连接错。必须在 import SDK 之前补齐。
_np = os.environ.get("no_proxy", "")
if "127.0.0.1" not in _np:
    _merged = ",".join(x for x in (_np, "127.0.0.1,localhost") if x)
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = _merged

REPO = Path(__file__).resolve().parents[1]

# ⚠️ 自己加载 .env（不依赖调用方）：load_dotenv 默认不覆盖已有环境变量，
#    所以在容器里用 env 注入时不会有副作用。
try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
except ImportError:  # 生产镜像里可能没装 dotenv：靠 env 注入即可
    pass

PROMPTS_DIR = REPO / "prompts"
DEFAULT_LABEL = os.getenv("PROMPT_LABEL", "production")

# 回落/超时三件套：registry 不可达时别让请求等成 90 秒（D15 同款教训）
FETCH_TIMEOUT = float(os.getenv("PROMPT_FETCH_TIMEOUT", "3"))
MAX_RETRIES = int(os.getenv("PROMPT_MAX_RETRIES", "0"))
CACHE_TTL = int(os.getenv("PROMPT_CACHE_TTL", "60"))  # 0 = 每次都拉（调试用）


def prompt_hash(text: str) -> str:
    """内容指纹：sha256 前 8 位。与 track_prompt_run.py 完全一致。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _labels_map(name: str) -> dict[str, int]:
    """读 prompts/<name>/labels.yaml —— 发布指针的 git 镜像（registry 才是真指针）。

    存在理由：回落时必须知道「production 当时指向哪版」，否则只能猜最新版。
    register_prompts.py 在拨 label 时会同步写这个文件。
    """
    f = PROMPTS_DIR / name / "labels.yaml"
    if not f.exists():
        return {}
    doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return {str(k): int(v) for k, v in doc.items() if v is not None}


def _local(
    name: str, version: int | None, label: str | None
) -> tuple[str, int, str, str, str]:
    """读本地文件，返回 (template, version, hash, source, local_pick)。

    取版顺序：version 指定 → labels.yaml 里的 label → 目录最大版本号。
    """
    d = PROMPTS_DIR / name
    f: Path | None = None
    pick = ""

    if version is not None:
        f = d / f"v{version}.yaml"
        pick = f"version={version}"
    else:
        labels = _labels_map(name)
        if label and label in labels:
            cand = d / f"v{labels[label]}.yaml"
            if cand.exists():
                f, pick = cand, f"label={label}→v{labels[label]}（labels.yaml）"
            else:
                pick = f"labels.yaml 说 {label}→v{labels[label]}，但文件缺失"
        if f is None:
            files = sorted(d.glob("v*.yaml"), key=lambda p: int(p.stem[1:]))
            if not files:
                raise FileNotFoundError(f"本地没有 {name} 的任何版本：{d}")
            f = files[-1]
            pick = (pick + "；" if pick else "") + f"latest=v{int(f.stem[1:])}"

    if not f.exists():
        raise FileNotFoundError(f"本地没有这个版本：{f}")
    doc: dict[str, Any] = yaml.safe_load(f.read_text(encoding="utf-8"))
    tpl = doc["template"]
    return tpl, int(doc["version"]), prompt_hash(tpl), f"local:{f.relative_to(REPO)}", pick


def load_prompt_local(
    name: str, *, version: int | None = None, label: str | None = None
) -> dict[str, Any]:
    """**只读本地 yaml**，不碰 registry（给「兜底」用，零网络、零超时）。

    用途：app/combos.py 等模块的 fallback 需要一份 prompt 原文，但它不能为此
    再打一次 registry（会叠加一次超时）。2026-10-21 起，代码里不再保留 prompt
    副本 —— 兜底也读 prompts/*.yaml，保证「唯一真源」（DEC-027）。
    """
    tpl, ver, h, src, pick = _local(name, version, label)
    return {
        "template": tpl,
        "version": ver,
        "hash": h,
        "source": src,
        "reason": None,
        "local_pick": pick,
        "client": None,
    }


def _registry(
    name: str, version: int | None, label: str | None
) -> tuple[str, int, str, str, Any]:
    """返回 (template, version, hash, source, client)。

    ⚠️ 第 5 个元素 client 是 Langfuse 的 PromptClient 对象：observability 会把它
       传给 start_as_current_generation(prompt=...)，这样 UI 的 Prompt Name 列
       才会填上、并支持按 prompt 版本联动筛选 trace（只写 metadata 做不到）。
    """
    """从 Langfuse 拉：按 version 或 label（二者只能选一个）。"""
    from langfuse import Langfuse  # 延迟导入：回落路径不付 import 开销

    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        # 早失败：与其让 SDK 打一句 Authentication error 再返回 400，
        # 不如把原因说清楚（这条正是 2026-10-21 四条路径全落地的根因）。
        raise RuntimeError("LANGFUSE_PUBLIC_KEY 为空：.env 没加载或变量名拼错")

    lf = Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        host=os.getenv("LANGFUSE_HOST"),
    )
    p = lf.get_prompt(
        name,
        version=version,
        label=None if version is not None else label,
        cache_ttl_seconds=CACHE_TTL,
        max_retries=MAX_RETRIES,
        fetch_timeout_seconds=FETCH_TIMEOUT,
    )
    tpl = p.prompt if isinstance(p.prompt, str) else str(p.prompt)
    return tpl, int(p.version), prompt_hash(tpl), "langfuse", p


def load_prompt(
    name: str,
    *,
    version: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """加载 prompt：registry 优先，失败回落本地文件。

    · version 给了就按版本钉死（复现实验用），此时忽略 label
    · 否则按 label（默认 production）取指针指向的版本
    """
    label = None if version is not None else (label or DEFAULT_LABEL)
    try:
        tpl, ver, h, src, client = _registry(name, version, label)
        return {
            "template": tpl,
            "version": ver,
            "hash": h,
            "source": src,
            "reason": None,
            "local_pick": None,
            "client": client,
        }
    except Exception as e:  # noqa: BLE001 —— 观测/registry 失败不能影响请求
        tpl, ver, h, src, pick = _local(name, version, label)
        return {
            "template": tpl,
            "version": ver,
            "hash": h,
            "source": src,
            "reason": f"{type(e).__name__}: {str(e)[:160]}",
            "local_pick": pick,
            "client": None,  # 回落时没有 registry 对象可挂
        }