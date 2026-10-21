"""W3 D15 · 可部署单元（combo）解析器：服务侧的「按 alias 取配置」

Registry 只负责「告诉部署系统该用哪个」，本模块负责「怎么把它读出来」。
核心取舍就是今天那道思考题：**alias 在启动时解析，还是每次请求解析？**

    · 启动时解析（本模块默认）：一次网络往返，之后零开销；
      代价是切 alias 不生效，要重启 / 等 TTL。
    · 每请求解析：改 alias 立刻生效（热切换）；
      代价是每发一次请求多一次 Registry 往返，且**同一次会话中途可能换 prompt**
      （客户端看到的「同一段对话前后不一致」比慢更难排查）。

取中间态：启动时解析 + COMBO_TTL_SECONDS 到期后重建（0 = 只解析一次）。

⚠️ 回落（fallback）不是偷懒：Registry 挂了 / 别名还没建 / 本地开发没起 server 时，
服务照样能起 —— 和 LLM_MODE=mock 一个思路。回落时 source="local-fallback"
**必须能看出来**，否则线上悄悄用了本地默认值都没人知道。

用法：
    cd app && python combos.py --alias production
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# ⚠️ 这三个必须写在 import mlflow 之前（MLflow 导入时读它们）。
# 实测（2026-10-20）：Registry 不可达时，默认配置让 load_combo **阻塞 90 秒以上**才回落 ——
# 「能回落」但回落得太慢，等于没有回落。收紧成：1 次重试、3 秒超时、无退避抖动。
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "3")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "0")

REGISTRY_NAME = os.getenv("COMBO_REGISTRY_NAME", "qa-combo")
DEFAULT_ALIAS = os.getenv("MODEL_ALIAS", "production")
COMBO_TTL_SECONDS = float(os.getenv("COMBO_TTL_SECONDS", "0"))

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 回落用的 prompt：**不再保留硬编码副本**（DEC-027：唯一真源是 prompts/*.yaml）。
# 这里只读本地文件、不碰 registry —— 兜底路径不该再叠一次网络超时。
def _fallback_prompt() -> str:
    try:
        import prompts  # 同目录模块

        return prompts.load_prompt_local("structured_json", version=1)["template"]
    except Exception:  # noqa: BLE001 —— 连文件都读不到时，宁可空串也不要瞎编
        return os.getenv("STRUCTURED_SYSTEM_PROMPT", "")


def _local_fallback(alias: str, reason: str) -> dict[str, Any]:
    return {
        "source": "local-fallback",
        "reason": reason,
        "alias": alias,
        "registry": REGISTRY_NAME,
        "prompt_version": "v0",
        "prompt_hash": "unknown",
        "model": os.getenv("LLM_PRIMARY_MODEL", ""),
        "temperature": 0.0,
        "max_tokens": 256,
        "system_prompt": _fallback_prompt(),
    }


def load_combo(alias: str | None = None, registry: str = REGISTRY_NAME) -> dict[str, Any]:
    """按 alias 取 combo。任何一步失败都回落到本地默认值（并说明原因）。"""
    alias = alias or DEFAULT_ALIAS
    with _lock:
        hit = _cache.get(alias)
        if hit and (COMBO_TTL_SECONDS <= 0 or time.time() - hit[0] < COMBO_TTL_SECONDS):
            return hit[1]

    try:
        import mlflow

        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
        base = f"models:/{registry}@{alias}"
        combo = mlflow.artifacts.load_dict(f"{base}/combo.json")
        combo["system_prompt"] = mlflow.artifacts.load_text(f"{base}/system_prompt.txt")
        combo["source"] = "mlflow-registry"
        combo["alias"] = alias
        combo["registry"] = registry
    except Exception as exc:  # noqa: BLE001 - 回落是设计目标，任何 Registry 故障都不能让服务起不来
        combo = _local_fallback(alias, f"{type(exc).__name__}: {exc}")

    with _lock:
        _cache[alias] = (time.time(), combo)
    return combo


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    ap.add_argument("--registry", default=REGISTRY_NAME)
    ap.add_argument("--json", action="store_true", help="只输出 JSON，方便管道")
    args = ap.parse_args()

    combo = load_combo(args.alias, args.registry)
    if args.json:
        print(json.dumps(combo, ensure_ascii=False, indent=2))
        return 0

    print(f"alias          : {combo.get('alias')}")
    print(f"source         : {combo.get('source')}")
    if combo.get("reason"):
        print(f"reason         : {combo['reason']}")
    for k in ("prompt_version", "prompt_hash", "model", "temperature", "max_tokens", "source_run_id"):
        print(f"{k:<16}: {combo.get(k)}")
    print(f"system_prompt  : {str(combo.get('system_prompt', ''))[:60]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())